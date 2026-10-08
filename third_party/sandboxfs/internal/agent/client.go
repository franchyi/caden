package agent

import (
	"context"
	"net/http"
	"time"

	"github.com/franchyi/sandboxfs/internal/rpc"
)

type Client struct {
	httpClient *http.Client
}

func NewClient(socketPath string, timeout time.Duration) *Client {
	return &Client{httpClient: rpc.UnixHTTPClient(socketPath, timeout)}
}

func (c *Client) Health(ctx context.Context) (rpc.HealthResponse, error) {
	var response rpc.HealthResponse
	err := rpc.DoJSON(ctx, c.httpClient, http.MethodGet, "/v1/health", nil, &response)
	return response, err
}

func (c *Client) Exec(ctx context.Context, request rpc.CommandRequest) (rpc.CommandResponse, error) {
	var response rpc.CommandResponse
	err := rpc.DoJSON(ctx, c.httpClient, http.MethodPost, "/v1/exec", request, &response)
	return response, err
}

func (c *Client) Shutdown(ctx context.Context) error {
	var response rpc.ShutdownResponse
	return rpc.DoJSON(ctx, c.httpClient, http.MethodPost, "/v1/shutdown", struct{}{}, &response)
}
