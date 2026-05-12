// Five-step example for the zkCEX Go SDK.
//
//	cd sdks/go
//	go run ./examples

package main

import (
	"errors"
	"fmt"
	"os"
	"time"

	zkcex "github.com/zkcex/zkcex-go/zkcex"
)

func main() {
	base := os.Getenv("ZKCEX_URL")
	if base == "" {
		base = "http://localhost:5500"
	}
	c := zkcex.New(base, "", "")

	// 1) Sign up — gives us a bearer token + a freshly-seeded wallet.
	email := fmt.Sprintf("sdk-go-%d@example.com", time.Now().Unix())
	me, err := c.Signup(email, "pass1234", "SDK Go demo")
	if err != nil {
		panic(err)
	}
	fmt.Println("signup user:", me.User.OpexUser)

	// 2) Public market data.
	info, err := c.ExchangeInfo()
	if err != nil {
		panic(err)
	}
	fmt.Println("exchange has", len(info.Symbols), "markets")

	depth, err := c.Depth("ETHUSDT", 5)
	if err != nil {
		panic(err)
	}
	fmt.Println("depth top ask:", depth.Asks[0])

	// 3) Issue an HMAC API key.
	key, err := c.CreateAPIKey(zkcex.CreateAPIKeyRequest{
		Label:  "sdk-go-demo",
		Scopes: []string{"read", "trade"},
	})
	if err != nil {
		panic(err)
	}
	c.APIKey = key.KeyID
	c.APISecret = key.Secret
	fmt.Println("api key:", key.KeyID)

	// 4) Signed call: /v3/account.
	acct, err := c.Account()
	if err != nil {
		panic(err)
	}
	fmt.Println("account balances:", acct.Balances)

	// 5) Place a tiny limit order.
	order, err := c.PlaceOrder(zkcex.PlaceOrderRequest{
		Symbol: "ETHUSDT", Side: "BUY", Type: "LIMIT",
		Quantity: "0.01", Price: "50", TimeInForce: "GTC",
	})
	if err != nil {
		var apiErr *zkcex.APIError
		if errors.As(err, &apiErr) {
			fmt.Println("place order rejected:", apiErr.Status, string(apiErr.Payload))
		} else {
			panic(err)
		}
	} else {
		fmt.Println("order:", order)
	}
}
