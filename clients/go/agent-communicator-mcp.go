package main

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	// For generating session ID
	"github.com/joho/godotenv" // For .env file support
	"github.com/r3labs/sse/v2" // For SSE client
)

// Configuration stores settings loaded from environment variables or .env file.
type Configuration struct {
	SSEServerBaseURL string `mapstructure:"SSE_SERVER_BASE_URL"`
	AuthToken        string `mapstructure:"AUTH_TOKEN"`
	LogLevel         string `mapstructure:"LOG_LEVEL"`
	AdapterProxyName string `mapstructure:"ADAPTER_PROXY_NAME"`
	SessionID        string // Will be generated
	MessagePostURL   string // Will be derived
	SSEStreamURL     string // Will be derived
}

// loadConfig loads configuration from environment variables.
// It also supports loading from a .env file if present.
func loadConfig() (*Configuration, error) {
	// Attempt to load .env file. Ignore error if it doesn't exist.
	_ = godotenv.Load()

	cfg := &Configuration{}
	cfg.SSEServerBaseURL = os.Getenv("ADAPTER_SSE_SERVER_BASE_URL")
	if cfg.SSEServerBaseURL == "" {
		return nil, fmt.Errorf("ADAPTER_SSE_SERVER_BASE_URL environment variable is required")
	}
	cfg.SSEServerBaseURL = strings.TrimRight(cfg.SSEServerBaseURL, "/")

	cfg.AuthToken = os.Getenv("ADAPTER_AUTH_TOKEN")
	cfg.LogLevel = os.Getenv("ADAPTER_LOG_LEVEL")
	if cfg.LogLevel == "" {
		cfg.LogLevel = "INFO"
	}
	cfg.AdapterProxyName = os.Getenv("ADAPTER_PROXY_NAME")
	if cfg.AdapterProxyName == "" {
		cfg.AdapterProxyName = "GoStdioSseProxy"
	}

	// Derive specific URLs
	cfg.SSEStreamURL = fmt.Sprintf("%s/sse", cfg.SSEServerBaseURL)

	return cfg, nil
}

// setupLogger configures the global logger.
func setupLogger(logLevel string) {
	level := strings.ToUpper(logLevel)
	log.SetOutput(os.Stderr) // Log to stderr, stdout is for MCP messages
	log.SetFlags(log.LstdFlags | log.Lshortfile)

	// Basic level setting (Go's standard log doesn't have fine-grained levels like Python's logging)
	// For more advanced logging, a library like zerolog or zap would be used.
	// Here, we just print everything if DEBUG, otherwise only standard logs.
	if level != "DEBUG" {
		// This is a bit of a hack for std logger; real libraries have proper level setting.
		// We can't easily suppress standard log.Print if not DEBUG without a custom logger.
	}
	log.Printf("Logger initialized with level: %s (Note: Go std logger has limited level filtering)", logLevel)
}

// readStdin reads lines from standard input and sends them to a channel.
// It closes the channel when stdin is closed or an error occurs.
func readStdin(ctx context.Context, stdinChan chan<- string, wg *sync.WaitGroup) {
	defer wg.Done()
	defer close(stdinChan)
	log.Println("[StdinReader] Starting to read from Stdin...")
	scanner := bufio.NewScanner(os.Stdin)
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" { // Skip empty lines if any
			continue
		}
		log.Printf("[StdinReader] Read line: %s", line)
		select {
		case stdinChan <- line:
		case <-ctx.Done():
			log.Println("[StdinReader] Context cancelled, stopping.")
			return
		}
	}
	if err := scanner.Err(); err != nil {
		if err != io.EOF {
			log.Printf("[StdinReader] Error reading from Stdin: %v", err)
		} else {
			log.Println("[StdinReader] Stdin closed (EOF).")
		}
	} else {
		log.Println("[StdinReader] Stdin stream finished.")
	}
}

// postMessages reads messages from a channel and POSTs them to the MCP server's message endpoint.
func postMessages(ctx context.Context, cfg *Configuration, stdinChan <-chan string, httpClient *http.Client, wg *sync.WaitGroup) {
	defer wg.Done()
	for len(cfg.MessagePostURL) == 0 {
		time.Sleep(100 * time.Millisecond)
	}
	log.Printf("[MsgPoster] Starting to post messages to %s", cfg.MessagePostURL)
	for {
		select {
		case line, ok := <-stdinChan:
			if !ok {
				log.Println("[MsgPoster] Stdin channel closed, stopping.")
				return
			}
			log.Printf("[MsgPoster] Sending to server: %s", line)

			req, err := http.NewRequestWithContext(ctx, "POST", cfg.MessagePostURL, bytes.NewBufferString(line))
			if err != nil {
				log.Printf("[MsgPoster] Error creating request: %v", err)
				continue // Or break/return depending on desired error handling
			}
			req.Header.Set("Content-Type", "application/json-rpc") // MCP standard
			if cfg.AuthToken != "" {
				req.Header.Set("Authorization", "Bearer "+cfg.AuthToken)
			}

			resp, err := httpClient.Do(req)
			if err != nil {
				log.Printf("[MsgPoster] Error POSTing message: %v", err)
				// Consider more robust error handling, e.g., retries or shutdown
				continue
			}
			// MCP POST responses are usually 202 Accepted with no critical content,
			// but good to check status.
			if resp.StatusCode >= 300 {
				log.Printf("[MsgPoster] Server responded with error status %d for message: %s", resp.StatusCode, line)
				// Optionally read and log resp.Body for more details
			}
			resp.Body.Close() // Important to close the response body

		case <-ctx.Done():
			log.Println("[MsgPoster] Context cancelled, stopping.")
			return
		}
	}
}

// Structure to help parse MCP InitializeResult
type MCPInitializeResult struct {
	SessionID string `json:"sessionId"`
	// ServerInfo could be parsed more fully if needed
}
type MCPResponse struct {
	JSONRPC string               `json:"jsonrpc"`
	ID      interface{}          `json:"id"`               // Can be string or number
	Result  *MCPInitializeResult `json:"result,omitempty"` // Pointer to make it optional
	Error   *interface{}         `json:"error,omitempty"`  // Can be any error structure
}

// receiveSSE connects to the SSE stream and forwards received messages to a channel.
// It closes the channel on error or shutdown.
func receiveSSE(ctx context.Context, cfg *Configuration, sseChan chan<- string, wg *sync.WaitGroup) {
	defer wg.Done()
	defer close(sseChan)
	log.Printf("[SSEReceiver] Connecting to SSE stream: %s", cfg.SSEStreamURL)

	// r3labs/sse/v2 client setup
	sseClient := sse.NewClient(cfg.SSEStreamURL)
	if cfg.AuthToken != "" {
		sseClient.Headers["Authorization"] = "Bearer " + cfg.AuthToken
	}
	sseClient.Connection.Transport = &http.Transport{
		Proxy: http.ProxyFromEnvironment, // Ensure proxy settings are respected
	}

	// Reconnection logic (optional, r3labs/sse might handle some of this)
	// For this example, we'll rely on the library's default or simple retry.
	// A more robust solution would use exponential backoff.
	eventsCtx, cancelEvents := context.WithCancel(ctx)

	defer cancelEvents() // Ensure subscription stops if this goroutine exits

	err := sseClient.SubscribeRawWithContext(eventsCtx, func(ev *sse.Event) {
		if len(ev.Data) == 0 {
			return // Skip empty keep-alive messages or similar
		}

		message := string(ev.Data)

		if len(cfg.MessagePostURL) == 0 && strings.HasPrefix(message, "/messages/") {
			log.Printf("[SSEReceiver] Deriving MessagePostURL from SSE message: %s", message)
			cfg.MessagePostURL = cfg.SSEServerBaseURL + message
		}

		log.Printf("[SSEReceiver] Received from server: %s", message)
		select {
		case sseChan <- message:
		case <-ctx.Done(): // Check outer context for shutdown signal
			log.Println("[SSEReceiver] Main context cancelled during send, stopping.")
			cancelEvents() // Stop SSE subscription
			return
		}
	})

	if err != nil && err != context.Canceled { // Ignore context.Canceled as it's expected on shutdown
		log.Printf("[SSEReceiver] SSE subscription error: %v", err)
	} else if err == context.Canceled {
		log.Println("[SSEReceiver] SSE subscription cancelled via context.")
	}
	log.Println("[SSEReceiver] SSE connection closed or subscription ended.")
}

// writeStdout reads messages from a channel and writes them to standard output.
func writeStdout(ctx context.Context, sseChan <-chan string, wg *sync.WaitGroup) {
	defer wg.Done()
	log.Println("[StdoutWriter] Starting to write messages to Stdout...")
	for {
		select {
		case line, ok := <-sseChan:
			if !ok {
				log.Println("[StdoutWriter] SSE channel closed, stopping.")
				return
			}
			// MCP messages are newline-terminated JSON
			fmt.Println(line)
			// Ensure it's flushed, especially if client expects immediate responses
			// For typical CLI interaction, Println might be sufficient.
		case <-ctx.Done():
			log.Println("[StdoutWriter] Context cancelled, stopping.")
			return
		}
	}
}

func main() {
	cfg, err := loadConfig()
	if err != nil {
		log.Fatalf("Failed to load configuration: %v", err)
	}
	setupLogger(cfg.LogLevel)
	log.Printf("Starting %s...", cfg.AdapterProxyName)
	log.Printf("Session ID: %s", cfg.SessionID)
	log.Printf("SSE Stream URL: %s", cfg.SSEStreamURL)
	log.Printf("Message POST URL: %s", cfg.MessagePostURL)

	httpClient := &http.Client{
		Timeout: 30 * time.Second, // Example timeout for POST requests
		Transport: &http.Transport{ // Ensure proxy settings are respected
			Proxy: http.ProxyFromEnvironment,
		},
	}

	// Context for managing goroutine lifecycles and shutdown
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel() // Ensure all children goroutines are signalled on exit

	var wg sync.WaitGroup

	stdinChan := make(chan string, 10) // Buffered channel
	sseChan := make(chan string, 10)   // Buffered channel

	// Start goroutines
	wg.Add(4)
	go readStdin(ctx, stdinChan, &wg)
	go postMessages(ctx, cfg, stdinChan, httpClient, &wg)
	go receiveSSE(ctx, cfg, sseChan, &wg)
	go writeStdout(ctx, sseChan, &wg)

	// Graceful shutdown handling
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	// Wait for a signal or for context to be cancelled (e.g., if a critical goroutine errors out)
	select {
	case s := <-sigChan:
		log.Printf("[Main] Received signal: %v. Shutting down...", s)
		cancel() // Signal all goroutines to stop
	case <-ctx.Done():
		log.Println("[Main] Context cancelled. Shutting down...")
		// This case might be hit if a goroutine calls cancel() due to an unrecoverable error
	}

	log.Println("[Main] Waiting for goroutines to finish...")
	// Give goroutines some time to shut down gracefully
	shutdownComplete := make(chan struct{})
	go func() {
		wg.Wait()
		close(shutdownComplete)
	}()

	select {
	case <-shutdownComplete:
		log.Println("[Main] All goroutines finished.")
	case <-time.After(5 * time.Second): // Timeout for shutdown
		log.Println("[Main] Timeout waiting for goroutines to finish. Forcing exit.")
	}

	log.Printf("%s shut down.", cfg.AdapterProxyName)
}

/**
**Key Features and Explanation:**

1.  **Configuration (`Configuration`, `loadConfig`)**:
    * Loads `ADAPTER_SSE_SERVER_BASE_URL` (required) and optional `ADAPTER_AUTH_TOKEN`, `ADAPTER_LOG_LEVEL`, `ADAPTER_PROXY_NAME` from environment variables.
    * Uses `github.com/joho/godotenv` to automatically load a `.env` file if present in the same directory.
    * Generates a unique `SessionID` (UUID v4) for this adapter instance. This ID is used in the path for POSTing messages to the SSE server, as per typical MCP SSE patterns.
    * Derives the full SSE streaming URL (e.g., `http://host/sse`) and the message POSTing URL (e.g., `http://host/messages/{sessionID}`).

2.  **Logging (`setupLogger`)**:
    * Sets up basic logging to `os.Stderr` (standard error) to keep `os.Stdout` (standard output) clear for MCP message protocol.
    * Includes timestamps and file/line numbers for log messages.

3.  **Goroutines for IO Operations**:
    * **`readStdin`**: Reads lines from `os.Stdin` (messages from the local MCP client like Claude Desktop) and sends them to a Go channel (`stdinChan`).
    * **`postMessages`**: Reads messages from `stdinChan` and POSTs them to the `cfg.MessagePostURL` of the remote SSE server.
        * Sets `Content-Type: application/json-rpc`.
        * Adds `Authorization: Bearer <token>` header if `cfg.AuthToken` is provided.
    * **`receiveSSE`**:
        * Uses `github.com/r3labs/sse/v2` to connect to `cfg.SSEStreamURL`.
        * Adds `Authorization` header if a token is provided.
        * Subscribes to raw SSE events. The `Data` field of each event is expected to be an MCP JSON-RPC message.
        * Sends received messages to another Go channel (`sseChan`).
    * **`writeStdout`**: Reads messages from `sseChan` and prints them to `os.Stdout`, making them available to the local MCP client.

4.  **Concurrency and Shutdown (`main`, `sync.WaitGroup`, `context.Context`)**:
    * Uses a `sync.WaitGroup` to ensure all goroutines can finish their work before the program exits.
    * Uses `context.Context` with cancellation to signal goroutines to shut down gracefully.
    * Listens for `SIGINT` (Ctrl+C) and `SIGTERM` signals to initiate shutdown.
    * Includes a timeout for the graceful shutdown process.

5.  **Error Handling**: Basic error logging is included in each goroutine. In a production system, error handling might involve more sophisticated retry mechanisms or signaling fatal errors to stop the entire adapter.

**To Compile and Run:**

1.  **Install Go:** If you don't have it, download and install Go from [golang.org](https://golang.org/).
2.  **Create Project Directory:**
    ```bash
    mkdir go-mcp-adapter
    cd go-mcp-adapter
    go mod init go-mcp-adapter
    ```
3.  **Install Dependencies:**
    ```bash
    go get github.com/google/uuid
    go get github.com/joho/godotenv
    go get github.com/r3labs/sse/v2
    ```
4.  **Save Code:** Save the Go code above as `main.go` in the `go-mcp-adapter` directory.
5.  **Configure:**
    * Create a `.env` file in the `go-mcp-adapter` directory:
        ```env
        ADAPTER_SSE_SERVER_BASE_URL="http://your-remote-mcp-server-base-url"
        # ADAPTER_AUTH_TOKEN="your_optional_bearer_token"
        # ADAPTER_LOG_LEVEL="DEBUG"
        ```
        Replace `http://your-remote-mcp-server-base-url` with the actual base URL of your SSE MCP server (e.g., `http://localhost:8000` if the SSE endpoint is `/sse`).
    * Or, set environment variables directly in your terminal.
6.  **Build:**
    ```bash
    go build -o mcp_adapter
    ```
7.  **Run:**
    ```bash
    ./mcp_adapter
    ```
8.  **Connect Client:** Configure your MCP client (e.g., Claude Desktop) to execute this compiled `mcp_adapter` program.

This Go adapter should now bridge communications between your STDIO-based MCP client and the remote SSE-based MCP server
**/
