package host

import (
	"context"
	"net/http"
	"net/url"
	"time"

	"github.com/franchyi/sandboxfs/internal/control"
	"github.com/franchyi/sandboxfs/internal/rpc"
)

type Client struct {
	httpClient *http.Client
}

func NewClient(socketPath string, timeout time.Duration) *Client {
	return &Client{httpClient: rpc.UnixHTTPClient(socketPath, timeout)}
}

func (c *Client) System(ctx context.Context) (control.SystemResponse, error) {
	var response control.SystemResponse
	err := rpc.DoJSON(ctx, c.httpClient, http.MethodGet, "/v1/system", nil, &response)
	return response, err
}

func (c *Client) RegisterBase(ctx context.Context, request control.RegisterBaseRequest) (control.BaseRecord, error) {
	var response control.BaseRecord
	err := rpc.DoJSON(ctx, c.httpClient, http.MethodPost, "/v1/bases", request, &response)
	return response, err
}

func (c *Client) VerifyBase(ctx context.Context, name string) (control.VerifyBaseResponse, error) {
	var response control.VerifyBaseResponse
	err := rpc.DoJSON(ctx, c.httpClient, http.MethodPost, "/v1/bases/"+url.PathEscape(name)+"/verify", struct{}{}, &response)
	return response, err
}

func (c *Client) ListBases(ctx context.Context) (control.ListBasesResponse, error) {
	var response control.ListBasesResponse
	err := rpc.DoJSON(ctx, c.httpClient, http.MethodGet, "/v1/bases", nil, &response)
	return response, err
}

func (c *Client) Create(ctx context.Context, request control.CreateSandboxRequest) (control.SandboxState, error) {
	var response control.SandboxState
	err := rpc.DoJSON(ctx, c.httpClient, http.MethodPost, "/v1/sandboxes", request, &response)
	return response, err
}

func (c *Client) Get(ctx context.Context, id string) (control.SandboxState, error) {
	var response control.SandboxState
	err := rpc.DoJSON(ctx, c.httpClient, http.MethodGet, "/v1/sandboxes/"+url.PathEscape(id), nil, &response)
	return response, err
}

func (c *Client) List(ctx context.Context) (control.ListSandboxesResponse, error) {
	var response control.ListSandboxesResponse
	err := rpc.DoJSON(ctx, c.httpClient, http.MethodGet, "/v1/sandboxes", nil, &response)
	return response, err
}

func (c *Client) Exec(ctx context.Context, id string, request control.ExecRequest) (control.ExecResponse, error) {
	var response control.ExecResponse
	err := rpc.DoJSON(ctx, c.httpClient, http.MethodPost, "/v1/sandboxes/"+url.PathEscape(id)+"/exec", request, &response)
	return response, err
}

func (c *Client) Destroy(ctx context.Context, id string) (control.DestroyResponse, error) {
	var response control.DestroyResponse
	err := rpc.DoJSON(ctx, c.httpClient, http.MethodDelete, "/v1/sandboxes/"+url.PathEscape(id), nil, &response)
	return response, err
}
