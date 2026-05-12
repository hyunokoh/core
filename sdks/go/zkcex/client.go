// Package zkcex is the official minimal Go SDK for the zkCEX REST API.
//
// Zero third-party dependencies — only the Go standard library.
//
// Five-line example:
//
//	c := zkcex.New("http://localhost:5500", "KEY", "SECRET")
//	depth, _ := c.Depth("ETHUSDT", 5)
//	fmt.Println(depth)
//	order, err := c.PlaceOrder(zkcex.PlaceOrderRequest{
//	    Symbol: "ETHUSDT", Side: "BUY", Type: "LIMIT",
//	    Quantity: "0.01", Price: "50", TimeInForce: "GTC",
//	})
package zkcex

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	DefaultBaseURL    = "http://localhost:5500"
	DefaultRecvWindow = 5000
	DefaultTimeout    = 30 * time.Second
)

// APIError is returned for any non-2xx HTTP response.
type APIError struct {
	Status  int
	URL     string
	Payload json.RawMessage
}

func (e *APIError) Error() string {
	return fmt.Sprintf("zkcex: HTTP %d from %s: %s", e.Status, e.URL, string(e.Payload))
}

// Client is the zkCEX REST client. Copy by value is safe — internal http
// client is shared by reference.
type Client struct {
	BaseURL      string
	APIKey       string
	APISecret    string
	SessionToken string
	RecvWindow   int
	UserAgent    string

	httpClient *http.Client
}

// New constructs a Client with sensible defaults. Pass empty strings for
// any credential you don't have yet.
func New(baseURL, apiKey, apiSecret string) *Client {
	if baseURL == "" {
		baseURL = DefaultBaseURL
	}
	return &Client{
		BaseURL:    strings.TrimRight(baseURL, "/"),
		APIKey:     apiKey,
		APISecret:  apiSecret,
		RecvWindow: DefaultRecvWindow,
		UserAgent:  "zkcex-go/1.0",
		httpClient: &http.Client{Timeout: DefaultTimeout},
	}
}

// ---- HTTP plumbing -------------------------------------------------------

type requestOpts struct {
	params url.Values
	body   any
	signed bool
	authed bool
}

func (c *Client) request(method, path string, opts requestOpts, out any) error {
	full := c.BaseURL + path
	var qs string
	if opts.signed {
		if c.APIKey == "" || c.APISecret == "" {
			return fmt.Errorf("zkcex: missing API key/secret for signed call")
		}
		qs = c.signQuery(opts.params)
	} else if len(opts.params) > 0 {
		qs = stripEmpty(opts.params).Encode()
	}
	if qs != "" {
		full = full + "?" + qs
	}

	var bodyReader io.Reader
	if opts.body != nil {
		bs, err := json.Marshal(opts.body)
		if err != nil {
			return err
		}
		bodyReader = bytes.NewReader(bs)
	}

	req, err := http.NewRequest(method, full, bodyReader)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", c.UserAgent)
	if bodyReader != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if opts.signed {
		req.Header.Set("X-MBX-APIKEY", c.APIKey)
	}
	if opts.authed {
		if c.SessionToken == "" {
			return fmt.Errorf("zkcex: missing session token for bearer call")
		}
		req.Header.Set("Authorization", "Bearer "+c.SessionToken)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode >= 400 {
		return &APIError{Status: resp.StatusCode, URL: full, Payload: raw}
	}
	if out == nil || len(raw) == 0 {
		return nil
	}
	return json.Unmarshal(raw, out)
}

func (c *Client) signQuery(params url.Values) string {
	if params == nil {
		params = url.Values{}
	}
	// Preserve insertion order via a stable key sort. Binance does NOT
	// require sorting, but doing so gives us deterministic signatures
	// regardless of map iteration order.
	keys := make([]string, 0, len(params))
	for k := range params {
		if len(params[k]) == 0 || params[k][0] == "" {
			continue
		}
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys)+2)
	for _, k := range keys {
		for _, v := range params[k] {
			if v == "" {
				continue
			}
			parts = append(parts, url.QueryEscape(k)+"="+url.QueryEscape(v))
		}
	}
	parts = append(parts, "timestamp="+strconv.FormatInt(time.Now().UnixMilli(), 10))
	parts = append(parts, "recvWindow="+strconv.Itoa(c.RecvWindow))
	qs := strings.Join(parts, "&")
	mac := hmac.New(sha256.New, []byte(c.APISecret))
	mac.Write([]byte(qs))
	return qs + "&signature=" + hex.EncodeToString(mac.Sum(nil))
}

func stripEmpty(in url.Values) url.Values {
	out := url.Values{}
	for k, v := range in {
		for _, item := range v {
			if item != "" {
				out.Add(k, item)
			}
		}
	}
	return out
}

func params(kv ...string) url.Values {
	v := url.Values{}
	for i := 0; i+1 < len(kv); i += 2 {
		if kv[i+1] != "" {
			v.Set(kv[i], kv[i+1])
		}
	}
	return v
}

// ---- Auth ---------------------------------------------------------------

func (c *Client) Signup(email, password, name string) (*AuthResponse, error) {
	var out AuthResponse
	err := c.request("POST", "/auth/signup", requestOpts{
		body: map[string]string{"email": email, "password": password, "name": name},
	}, &out)
	if err == nil && out.Token != "" {
		c.SessionToken = out.Token
	}
	return &out, err
}

func (c *Client) Login(email, password string) (*AuthResponse, error) {
	var out AuthResponse
	err := c.request("POST", "/auth/login", requestOpts{
		body: map[string]string{"email": email, "password": password},
	}, &out)
	if err == nil && out.Token != "" {
		c.SessionToken = out.Token
	}
	return &out, err
}

func (c *Client) Me() (*User, error) {
	var out struct {
		User User `json:"user"`
	}
	if err := c.request("GET", "/auth/me", requestOpts{authed: true}, &out); err != nil {
		return nil, err
	}
	return &out.User, nil
}

func (c *Client) Logout() error {
	err := c.request("POST", "/auth/logout", requestOpts{authed: true}, nil)
	if err == nil {
		c.SessionToken = ""
	}
	return err
}

func (c *Client) AuthHealth() (*AuthHealth, error) {
	var out AuthHealth
	return &out, c.request("GET", "/auth/health", requestOpts{}, &out)
}

// ---- Spot market --------------------------------------------------------

func (c *Client) ExchangeInfo() (*SpotExchangeInfo, error) {
	var out SpotExchangeInfo
	return &out, c.request("GET", "/v3/exchangeInfo", requestOpts{}, &out)
}

func (c *Client) Depth(symbol string, limit int) (*Depth, error) {
	var out Depth
	return &out, c.request("GET", "/v3/depth", requestOpts{
		params: params("symbol", symbol, "limit", strconv.Itoa(limit)),
	}, &out)
}

func (c *Client) Klines(symbol, interval string, limit int) ([]Kline, error) {
	var out []Kline
	return out, c.request("GET", "/v3/klines", requestOpts{
		params: params("symbol", symbol, "interval", interval,
			"limit", strconv.Itoa(limit)),
	}, &out)
}

func (c *Client) RecentTrades(symbol string, limit int) ([]Trade, error) {
	var out []Trade
	return out, c.request("GET", "/v3/trades", requestOpts{
		params: params("symbol", symbol, "limit", strconv.Itoa(limit)),
	}, &out)
}

func (c *Client) Ticker24h(symbol string) (json.RawMessage, error) {
	var out json.RawMessage
	return out, c.request("GET", "/v3/ticker/24hr", requestOpts{
		params: params("symbol", symbol),
	}, &out)
}

// ---- Spot trade (signed) ------------------------------------------------

// PlaceOrderRequest is the input to PlaceOrder. Mirror of the Binance shape.
type PlaceOrderRequest struct {
	Symbol           string
	Side             string // BUY | SELL
	Type             string // LIMIT | MARKET | ...
	Quantity         string
	Price            string
	TimeInForce      string // GTC | IOC | FOK (LIMIT only)
	QuoteOrderQty    string
	NewClientOrderID string
}

func (c *Client) PlaceOrder(r PlaceOrderRequest) (*Order, error) {
	p := url.Values{}
	p.Set("symbol", r.Symbol)
	p.Set("side", r.Side)
	p.Set("type", r.Type)
	if r.Quantity != "" {
		p.Set("quantity", r.Quantity)
	}
	if r.Price != "" {
		p.Set("price", r.Price)
	}
	if strings.ToUpper(r.Type) == "LIMIT" {
		tif := r.TimeInForce
		if tif == "" {
			tif = "GTC"
		}
		p.Set("timeInForce", tif)
	}
	if r.QuoteOrderQty != "" {
		p.Set("quoteOrderQty", r.QuoteOrderQty)
	}
	if r.NewClientOrderID != "" {
		p.Set("newClientOrderId", r.NewClientOrderID)
	}
	var out Order
	return &out, c.request("POST", "/v3/order",
		requestOpts{params: p, signed: true}, &out)
}

func (c *Client) CancelOrder(symbol string, orderID int64, origClientOrderID string) (*Order, error) {
	p := params("symbol", symbol, "origClientOrderId", origClientOrderID)
	if orderID > 0 {
		p.Set("orderId", strconv.FormatInt(orderID, 10))
	}
	var out Order
	return &out, c.request("DELETE", "/v3/order",
		requestOpts{params: p, signed: true}, &out)
}

func (c *Client) OpenOrders(symbol string) ([]Order, error) {
	p := url.Values{}
	if symbol != "" {
		p.Set("symbol", symbol)
	}
	var out []Order
	return out, c.request("GET", "/v3/openOrders",
		requestOpts{params: p, signed: true}, &out)
}

func (c *Client) MyTrades(symbol string, limit int) ([]Trade, error) {
	var out []Trade
	return out, c.request("GET", "/v3/myTrades", requestOpts{
		params: params("symbol", symbol, "limit", strconv.Itoa(limit)),
		signed: true,
	}, &out)
}

func (c *Client) Account() (*Account, error) {
	var out Account
	return &out, c.request("GET", "/v3/account",
		requestOpts{params: url.Values{}, signed: true}, &out)
}

func (c *Client) Withdraw(asset, amount, address, network string) (json.RawMessage, error) {
	var out json.RawMessage
	return out, c.request("POST", "/v3/withdraw", requestOpts{
		params: params("asset", asset, "amount", amount,
			"address", address, "network", network),
		signed: true,
	}, &out)
}

// ---- Futures ------------------------------------------------------------

func (c *Client) FuturesExchangeInfo() (*FuturesExchangeInfo, error) {
	var out FuturesExchangeInfo
	return &out, c.request("GET", "/fapi/v1/exchangeInfo", requestOpts{}, &out)
}

func (c *Client) PremiumIndex(symbol string) (json.RawMessage, error) {
	var out json.RawMessage
	return out, c.request("GET", "/fapi/v1/premiumIndex", requestOpts{
		params: params("symbol", symbol),
	}, &out)
}

func (c *Client) FuturesAccount() (*FuturesAccount, error) {
	var out FuturesAccount
	return &out, c.request("GET", "/fapi/v1/account",
		requestOpts{params: url.Values{}, signed: true}, &out)
}

func (c *Client) PositionRisk(symbol string) ([]Position, error) {
	p := url.Values{}
	if symbol != "" {
		p.Set("symbol", symbol)
	}
	var out []Position
	return out, c.request("GET", "/fapi/v1/positionRisk",
		requestOpts{params: p, signed: true}, &out)
}

// FuturesOrderRequest is the input to FuturesPlaceOrder.
type FuturesOrderRequest struct {
	Symbol           string
	Side             string
	Type             string
	Quantity         string
	Price            string
	TimeInForce      string
	ReduceOnly       string // "true" | "false" | ""
	PositionSide     string
	NewClientOrderID string
}

func (c *Client) FuturesPlaceOrder(r FuturesOrderRequest) (*Order, error) {
	p := url.Values{}
	p.Set("symbol", r.Symbol)
	p.Set("side", r.Side)
	p.Set("type", r.Type)
	p.Set("quantity", r.Quantity)
	if r.Price != "" {
		p.Set("price", r.Price)
	}
	if r.TimeInForce != "" {
		p.Set("timeInForce", r.TimeInForce)
	}
	if r.ReduceOnly != "" {
		p.Set("reduceOnly", r.ReduceOnly)
	}
	if r.PositionSide != "" {
		p.Set("positionSide", r.PositionSide)
	}
	if r.NewClientOrderID != "" {
		p.Set("newClientOrderId", r.NewClientOrderID)
	}
	var out Order
	return &out, c.request("POST", "/fapi/v1/order",
		requestOpts{params: p, signed: true}, &out)
}

func (c *Client) FuturesClosePosition(symbol string) (*Order, error) {
	var out Order
	return &out, c.request("POST", "/fapi/v1/closePosition", requestOpts{
		params: params("symbol", symbol), signed: true,
	}, &out)
}

func (c *Client) FuturesSetLeverage(symbol string, leverage int) (*LeverageResponse, error) {
	var out LeverageResponse
	return &out, c.request("POST", "/fapi/v1/leverage", requestOpts{
		params: params("symbol", symbol,
			"leverage", strconv.Itoa(leverage)),
		signed: true,
	}, &out)
}

func (c *Client) FuturesTransfer(asset, amount string, kind int) (json.RawMessage, error) {
	var out json.RawMessage
	return out, c.request("POST", "/fapi/v1/transfer", requestOpts{
		params: params("asset", asset, "amount", amount,
			"type", strconv.Itoa(kind)),
		signed: true,
	}, &out)
}

// ---- Chain --------------------------------------------------------------

func (c *Client) ChainInfo() (json.RawMessage, error) {
	var out json.RawMessage
	return out, c.request("GET", "/chain/info", requestOpts{}, &out)
}

func (c *Client) ChainWallet(chain string) (json.RawMessage, error) {
	var out json.RawMessage
	return out, c.request("GET", "/chain/wallet", requestOpts{
		params: params("chain", chain),
		authed: true,
	}, &out)
}

func (c *Client) ChainWithdraw(asset, amount, toAddress, chain string) (json.RawMessage, error) {
	var out json.RawMessage
	body := map[string]string{
		"asset": asset, "amount": amount, "to_address": toAddress,
	}
	if chain != "" {
		body["chain"] = chain
	}
	return out, c.request("POST", "/chain/withdraw",
		requestOpts{body: body, authed: true}, &out)
}

// ---- PoL ---------------------------------------------------------------

func (c *Client) PolServerInfo() (json.RawMessage, error) {
	var out json.RawMessage
	return out, c.request("GET", "/pol/server-info", requestOpts{}, &out)
}

func (c *Client) PolMyProof() (json.RawMessage, error) {
	var out json.RawMessage
	return out, c.request("GET", "/pol/my-proof",
		requestOpts{authed: true}, &out)
}

func (c *Client) ReservesVsLiabilities() (json.RawMessage, error) {
	var out json.RawMessage
	return out, c.request("GET", "/pol/reserves-vs-liabilities",
		requestOpts{}, &out)
}

// ---- API keys ----------------------------------------------------------

// CreateAPIKeyRequest is the body for CreateAPIKey.
type CreateAPIKeyRequest struct {
	Label          string   `json:"label"`
	Scopes         []string `json:"scopes,omitempty"`
	ExpiresInDays  int      `json:"expires_in_days,omitempty"`
	IPAllowlist    string   `json:"ip_allowlist,omitempty"`
	ConfirmPhrase  string   `json:"confirm_phrase,omitempty"`
	DailyQuoteCap  string   `json:"daily_quote_cap_usdt,omitempty"`
	HourlyReqCap   int      `json:"hourly_request_cap,omitempty"`
}

// CreateAPIKeyResponse is what CreateAPIKey returns. The Secret is the
// only chance you have to read the raw HMAC secret.
type CreateAPIKeyResponse struct {
	KeyID      string   `json:"key_id"`
	Secret     string   `json:"secret"`
	Scopes     []string `json:"scopes"`
	ExpiresAt  *int64   `json:"expires_at"`
	Label      string   `json:"label"`
}

func (c *Client) CreateAPIKey(r CreateAPIKeyRequest) (*CreateAPIKeyResponse, error) {
	if r.Scopes == nil {
		r.Scopes = []string{"read"}
	}
	if r.ExpiresInDays == 0 {
		r.ExpiresInDays = 90
	}
	var out CreateAPIKeyResponse
	return &out, c.request("POST", "/api-keys/create",
		requestOpts{body: r, authed: true}, &out)
}

func (c *Client) ListAPIKeys() (json.RawMessage, error) {
	var out json.RawMessage
	return out, c.request("GET", "/api-keys/list",
		requestOpts{authed: true}, &out)
}

func (c *Client) RevokeAPIKey(keyID string) error {
	return c.request("DELETE", "/api-keys/"+url.PathEscape(keyID),
		requestOpts{authed: true}, nil)
}

// ---- Conditional orders ------------------------------------------------

func (c *Client) ConditionalOrder(body any) (json.RawMessage, error) {
	var out json.RawMessage
	return out, c.request("POST", "/orders/conditional",
		requestOpts{body: body, authed: true}, &out)
}

func (c *Client) ListConditionals() (json.RawMessage, error) {
	var out json.RawMessage
	return out, c.request("GET", "/orders/conditional",
		requestOpts{authed: true}, &out)
}

func (c *Client) CancelConditional(clientOrderID string) error {
	return c.request("DELETE",
		"/orders/conditional/"+url.PathEscape(clientOrderID),
		requestOpts{authed: true}, nil)
}
