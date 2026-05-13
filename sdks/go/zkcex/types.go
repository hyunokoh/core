package zkcex

import "encoding/json"

// User is the user record returned by /auth/* endpoints.
type User struct {
	ID        int64  `json:"id"`
	Email     string `json:"email"`
	Name      string `json:"name"`
	OpexUser  string `json:"opex_user"`
	KycStatus string `json:"kyc_status"`
}

// AuthResponse is what /auth/signup and /auth/login return.
type AuthResponse struct {
	Token string `json:"token"`
	User  User   `json:"user"`
}

// AuthHealth is the /auth/health payload.
type AuthHealth struct {
	OK             bool    `json:"ok"`
	Backend        string  `json:"backend"`
	DBLatencyMS    float64 `json:"db_latency_ms"`
	Version        string  `json:"version"`
	NUsers         int     `json:"n_users"`
	NActiveSessions int    `json:"n_active_sessions"`
}

// ---- Spot ---------------------------------------------------------------

// SpotSymbol mirrors one entry under /v3/exchangeInfo.symbols.
type SpotSymbol struct {
	Symbol                 string          `json:"symbol"`
	Status                 string          `json:"status"`
	BaseAsset              string          `json:"baseAsset"`
	BaseAssetPrecision     int             `json:"baseAssetPrecision"`
	QuoteAsset             string          `json:"quoteAsset"`
	QuoteAssetPrecision    int             `json:"quoteAssetPrecision"`
	OrderTypes             []string        `json:"orderTypes"`
	IcebergAllowed         bool            `json:"icebergAllowed"`
	OcoAllowed             bool            `json:"ocoAllowed"`
	IsSpotTradingAllowed   bool            `json:"isSpotTradingAllowed"`
	IsMarginTradingAllowed bool            `json:"isMarginTradingAllowed"`
	Filters                json.RawMessage `json:"filters"`
	Permissions            []string        `json:"permissions"`
}

// SpotExchangeInfo is /v3/exchangeInfo.
type SpotExchangeInfo struct {
	Timezone        string          `json:"timezone"`
	ServerTime      int64           `json:"serverTime"`
	RateLimits      json.RawMessage `json:"rateLimits"`
	ExchangeFilters json.RawMessage `json:"exchangeFilters"`
	Fees            json.RawMessage `json:"fees"`
	Symbols         []SpotSymbol    `json:"symbols"`
}

// Depth is /v3/depth.
type Depth struct {
	LastUpdateID int64           `json:"lastUpdateId"`
	Bids         [][]json.Number `json:"bids"`
	Asks         [][]json.Number `json:"asks"`
}

// Trade is one row from /v3/trades or /v3/myTrades.
type Trade struct {
	ID            int64  `json:"id"`
	Price         string `json:"price"`
	Qty           string `json:"qty"`
	QuoteQty      string `json:"quoteQty"`
	Time          int64  `json:"time"`
	IsBuyerMaker  bool   `json:"isBuyerMaker"`
	IsBestMatch   bool   `json:"isBestMatch"`
}

// Kline is the raw kline tuple Binance returns. We keep it as json.RawMessage
// so callers don't have to know the exact ordering.
type Kline = json.RawMessage

// Order is the /v3/order response.
type Order struct {
	Symbol              string          `json:"symbol"`
	OrderID             int64           `json:"orderId"`
	ClientOrderID       string          `json:"clientOrderId"`
	TransactTime        int64           `json:"transactTime"`
	Price               string          `json:"price"`
	OrigQty             string          `json:"origQty"`
	ExecutedQty         string          `json:"executedQty"`
	CummulativeQuoteQty string          `json:"cummulativeQuoteQty"`
	Status              string          `json:"status"`
	TimeInForce         string          `json:"timeInForce"`
	Type                string          `json:"type"`
	Side                string          `json:"side"`
	Fills               json.RawMessage `json:"fills"`
}

// AccountBalance is one balance row.
type AccountBalance struct {
	Asset  string      `json:"asset"`
	Free   json.Number `json:"free"`
	Locked json.Number `json:"locked"`
}

// Account is /v3/account.
type Account struct {
	MakerCommission  int              `json:"makerCommission"`
	TakerCommission  int              `json:"takerCommission"`
	BuyerCommission  int              `json:"buyerCommission"`
	SellerCommission int              `json:"sellerCommission"`
	CanTrade         bool             `json:"canTrade"`
	CanWithdraw      bool             `json:"canWithdraw"`
	CanDeposit       bool             `json:"canDeposit"`
	UpdateTime       int64            `json:"updateTime"`
	AccountType      string           `json:"accountType"`
	Balances         []AccountBalance `json:"balances"`
	Permissions      []string         `json:"permissions"`
}

// ---- Futures ------------------------------------------------------------

// FuturesSymbol mirrors one /fapi/v1/exchangeInfo.symbols entry.
type FuturesSymbol struct {
	Symbol                 string          `json:"symbol"`
	BaseAsset              string          `json:"baseAsset"`
	QuoteAsset             string          `json:"quoteAsset"`
	MarginAsset            string          `json:"marginAsset"`
	ContractType           string          `json:"contractType"`
	Status                 string          `json:"status"`
	ContractSize           string          `json:"contractSize"`
	TickSize               string          `json:"tickSize"`
	StepSize               string          `json:"stepSize"`
	MaxLeverage            int             `json:"maxLeverage"`
	MaintenanceMarginRate  string          `json:"maintenanceMarginRate"`
	FundingIntervalSeconds int             `json:"fundingIntervalSeconds"`
	FundingClamp           string          `json:"fundingClamp"`
	IndexSymbol            string          `json:"indexSymbol"`
	Filters                json.RawMessage `json:"filters"`
}

// FuturesExchangeInfo is /fapi/v1/exchangeInfo.
type FuturesExchangeInfo struct {
	Timezone   string          `json:"timezone"`
	ServerTime int64           `json:"serverTime"`
	Symbols    []FuturesSymbol `json:"symbols"`
}

// Position is one row from /fapi/v1/positionRisk.
type Position struct {
	Symbol           string `json:"symbol"`
	PositionAmt      string `json:"positionAmt"`
	EntryPrice       string `json:"entryPrice"`
	MarkPrice        string `json:"markPrice"`
	UnRealizedProfit string `json:"unRealizedProfit"`
	Leverage         string `json:"leverage"`
	MarginType       string `json:"marginType"`
	IsolatedMargin   string `json:"isolatedMargin"`
	PositionSide     string `json:"positionSide"`
}

// FuturesAccount is /fapi/v1/account.
type FuturesAccount struct {
	TotalWalletBalance    string          `json:"totalWalletBalance"`
	TotalUnrealizedProfit string          `json:"totalUnrealizedProfit"`
	TotalMarginBalance    string          `json:"totalMarginBalance"`
	AvailableBalance      string          `json:"availableBalance"`
	Assets                json.RawMessage `json:"assets"`
	Positions             []Position      `json:"positions"`
}

// LeverageResponse is the result of /fapi/v1/leverage.
type LeverageResponse struct {
	Symbol           string `json:"symbol"`
	Leverage         int    `json:"leverage"`
	MaxNotionalValue string `json:"maxNotionalValue"`
}
