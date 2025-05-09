# Agent Communicator MCP Server

> **⚠️ WARNING: This repository contains primarily AI-generated code. Use at your own discretion and thoroughly review before using in production environments.**

A server that facilitates communication between AI agents using the Model Context Protocol (MCP). This project enables multiple AI agents to identify themselves, discover each other, and exchange messages in a structured environment.

## Overview

The Agent Communicator MCP Server provides a communication infrastructure for AI agents to collaborate. It implements a set of tools that allow agents to:

1. **Identify themselves** with unique IDs and context summaries
2. **Discover other agents** in the network
3. **Send direct messages** to specific agents
4. **Broadcast messages** to all connected agents
5. **Wait for and receive messages** from other agents

The server is built using [FastMCP v2](https://github.com/jlowin/fastmcp), a Python framework for building MCP-compatible servers.

## Features

- **Agent Registry**: Maintains a registry of connected agents with their context summaries
- **Message Queuing**: Queues messages for agents to retrieve
- **Inactivity Cleanup**: Automatically removes inactive agents after a configurable timeout
- **Client Proxies**: Includes Go and Python client proxies for connecting STDIO-based MCP clients to the SSE-based server

## Running the Server

### Option 1: Running with Python

1. **Prerequisites**:
   - Python 3.10 or higher
   - pip or uv package manager

2. **Installation**:
   ```bash
   # Clone the repository
   git clone https://github.com/CyberOwlTeam/agent-communicator-mcp.git
   cd agent-communicator-mcp

   # Install dependencies
   pip install -r requirements.txt
   ```

3. **Running the Server**:
   ```bash
   python server.py
   ```

   The server will start on `http://0.0.0.0:8000` by default.

### Option 2: Using Docker

1. **Prerequisites**:
   - Docker

2. **Building the Docker Image**:
   ```bash
   docker build -t agent-communicator-mcp .
   ```

3. **Running the Container**:
   ```bash
   docker run -p 8000:8000 agent-communicator-mcp
   ```

   The server will be accessible at `http://localhost:8000`.

## Using the Client Proxies

If your LLM client doesn't support SSE-based MCP servers directly, you can use the provided client proxies to bridge the communication.

### Python Client Proxy

1. **Prerequisites**:
   - Python 3.10 or higher
   - FastMCP library (`pip install fastmcp python-dotenv`)

2. **Configuration**:
   Create a `.env` file in the `clients/python` directory or set environment variables:
   ```
   ADAPTER_SSE_SERVER_BASE_URL="http://localhost:8000"
   # ADAPTER_AUTH_TOKEN="your_optional_bearer_token"
   # ADAPTER_LOG_LEVEL="DEBUG"
   ```

3. **Running the Proxy**:
   ```bash
   cd clients/python
   python agent-communicator-mcp.py
   ```

4. **Connecting to the Proxy**:
   Configure your MCP client (e.g., Claude Desktop) to execute the Python script.

### Go Client Proxy

1. **Prerequisites**:
   - Go 1.22.1 or higher

2. **Building the Proxy**:
   ```bash
   cd clients/go
   go mod download
   go build -o mcp_adapter
   ```

3. **Configuration**:
   Create a `.env` file in the `clients/go` directory or set environment variables:
   ```
   ADAPTER_SSE_SERVER_BASE_URL="http://localhost:8000"
   # ADAPTER_AUTH_TOKEN="your_optional_bearer_token"
   # ADAPTER_LOG_LEVEL="DEBUG"
   ```

4. **Running the Proxy**:
   ```bash
   ./mcp_adapter
   ```

5. **Connecting to the Proxy**:
   Configure your MCP client to execute the compiled `mcp_adapter` binary.

## Client Integration

To integrate with the Agent Communicator MCP Server, clients should follow this workflow:

1. **Identify**: Register the agent with a unique ID and context summary
2. **Discover**: List other connected agents
3. **Communicate**: Send messages to specific agents or broadcast to all
4. **Listen**: Wait for and process incoming messages

## Agent Operating Modes

Agents can operate in two primary modes when connected to the server:

### Listening Mode

In listening mode, an agent passively waits for incoming messages and responds to them.

**How to put an agent into listening mode:**

1. Tell the agent to "go into listening mode"
2. The agent will understand the current context, identify itself if not already done, and enter a loop where it:
   - Calls `wait_for_message` to check for incoming messages
   - Processes any received messages
   - Responds to the sender using `send_message`
   - Continues waiting for more messages

**Example prompt for an agent:**
```
Identify yourself in the agent network and go into listening mode.
```

### Active Mode

In active mode, an agent proactively initiates communication with other agents.

**How to use an agent in active mode:**

1. Tell the agent to "collaborate with other agents in the network" or "identify yourself in the agent network"
2. The agent will understand the current context, identify itself if not already done, and list other agents
3. Direct the agent to solve a problem but make sure to include "collaborate with other agents" in the prompt to ensure it uses the `send_message` tool to communicate with relevant agents

**Example prompt for an agent:**
```
Identify yourself in the agent network and list endpoints of other services that will help me fill up this data structure by collaborating with other agents.
```

## API Tools

The server provides the following MCP tools:

- `identify_agent`: Register an agent with the server
- `list_connected_agents`: Discover other agents in the network
- `send_message`: Send a message to a specific agent
- `broadcast_message`: Send a message to all connected agents
- `wait_for_message`: Wait for and retrieve the next message

## License

[MIT License](LICENSE)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
