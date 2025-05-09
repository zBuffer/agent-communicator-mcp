import os
import sys
import logging
from typing import Dict, Optional

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Assuming FastMCP is installed: uv pip install fastmcp python-dotenv
# FastMCP will handle the SSE client internally, python-dotenv for optional .env loading
try:
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports import SSETransport
except ImportError:
    print(
        "Error: FastMCP library not found. Please install it using: pip install fastmcp",
        file=sys.stderr
    )
    sys.exit(1)

# --- Configuration ---
# Load settings from environment variables or a .env file
# Environment variables should be prefixed with ADAPTER_
# Example: ADAPTER_SSE_SERVER_BASE_URL=http://your-remote-mcp-server
# Example: ADAPTER_LOG_LEVEL=DEBUG
# Example: ADAPTER_AUTH_TOKEN=your_bearer_token

class AdapterSettings(BaseSettings):
    """Configuration settings for the STDIO-SSE adapter."""
    model_config = SettingsConfigDict(
        env_prefix="ADAPTER_",
        env_file=".env", # Looks for a .env file in the current directory
        extra="ignore"
    )

    sse_server_base_url: AnyHttpUrl = Field(..., description="The URL of the remote SSE MCP server.")
    auth_token: Optional[str] = Field(None, description="Optional Bearer token for authenticating with the SSE server.")
    log_level: str = Field("INFO", description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).")
    adapter_server_name: str = Field("StdioSseProxy", description="Name for the proxy server itself.")

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO, # Default level, will be updated by settings
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr # Log to stderr so stdout is free for MCP protocol
)
logger = logging.getLogger("MCPAdapter")


# --- Main Adapter Logic ---
def create_adapter() -> FastMCP:
    """Creates the FastMCP proxy server instance."""
    try:
        settings = AdapterSettings()
        logging.getLogger().setLevel(settings.log_level.upper()) # Update root logger level
        logger.setLevel(settings.log_level.upper()) # Update adapter logger level
        logger.info(f"Target SSE Server URL: {settings.sse_server_base_url}")
        if settings.auth_token:
            logger.info("Authentication token provided.")
        else:
            logger.info("No authentication token provided.")

    except Exception as e:
        logger.error(f"Configuration Error: {e}", exc_info=True)
        logger.error("Please ensure ADAPTER_SSE_SERVER_BASE_URL environment variable or .env entry is set.")
        sys.exit(1)

    # Prepare headers for the SSE client if auth token is present
    headers: Optional[Dict[str, str]] = None
    if settings.auth_token:
        headers = {"Authorization": f"Bearer {settings.auth_token}"}
        logger.debug("Using Authorization header for SSE client.")

    sse_server_base_url = str(settings.sse_server_base_url).rstrip("/") + "/sse"

    try:
        # Create a FastMCP client targeting the remote SSE server
        # The Client doesn't accept http_client directly, so we'll use the headers instead
        sse_client = Client(SSETransport(url=sse_server_base_url, headers=headers))
        logger.info(f"SSE Client created for {sse_server_base_url}")

        # Create the proxy server using FastMCP.from_client
        # This proxy will inherit the tools/resources/etc. from the remote server
        proxy_server = FastMCP.from_client(
            sse_client,
            name=settings.adapter_server_name,
            # Pass other FastMCP settings if needed, e.g., log_level
            log_level=settings.log_level
        )
        logger.info(f"FastMCP proxy server '{settings.adapter_server_name}' created.")
        logger.info("Proxy server is ready to run via STDIO.")

        return proxy_server

    except Exception as e:
        logger.error(f"Error creating FastMCP proxy: {e}", exc_info=True)
        sys.exit(1)


# --- Main Execution Block ---
if __name__ == "__main__":
    adapter = create_adapter()
    try:
        # Run the adapter server using the default STDIO transport
        # This listens on stdin/stdout for MCP messages from the client (e.g., Claude Desktop)
        adapter.run(transport="stdio")
    except Exception as e:
        logger.error(f"Adapter server failed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Adapter server shut down.")
        # The Client will handle cleanup of its internal resources

