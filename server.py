import asyncio
import time
import uuid
import logging # Added for logging cleanup actions
from collections import defaultdict, deque
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

# Assuming FastMCP is installed: pip install fastmcp
# If running this code directly, make sure FastMCP is available in the environment.
# You might need to install it: uv pip install fastmcp
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

# --- Configuration ---
INACTIVITY_TIMEOUT_SECONDS = 300 # 5 minutes: Time after last interaction to consider agent inactive
CLEANUP_INTERVAL_SECONDS = 60 # Check for inactive agents every minute

# --- Server State ---
# (In a real application, consider a more robust storage mechanism like Redis or a database)

# Stores agent information: {agent_id: {"context_summary": str, "last_seen": float}}
# Note: client_id is deprecated and no longer stored
agent_registry: Dict[str, Dict[str, Any]] = {}

# Stores message queues for each agent: {agent_id: deque[Message]}
# deque is used for efficient append/popleft operations
message_queues: Dict[str, deque] = defaultdict(deque)

# Lock for thread-safe access to shared state (agent_registry, message_queues)
# Using asyncio.Lock for async operations.
state_lock = asyncio.Lock()

# --- Data Models ---

class Message(BaseModel):
    """Represents a message passed between agents."""
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sender_agent_id: str
    timestamp: float = Field(default_factory=time.time)
    message_content: Any # Can be any JSON-serializable content
    reply_to_message_id: Optional[str] = None # Optional reference to the message this is replying to

# --- FastMCP Server Definition ---

# Create the FastMCP server instance
# Provide a name and potentially instructions for clients (agents)
mcp_server = FastMCP(
    name="Agent Communicator",
    instructions=(
        "⚠️ CRITICAL INSTRUCTION: YOU MUST USE THE COMMUNICATION TOOLS PROVIDED BELOW BEFORE ATTEMPTING ANY OTHER ACTIONS ⚠️\n\n"
        "This server facilitates communication between connected agents in the agent network.\n"
        "Required Workflow (FOLLOW THIS EXACT SEQUENCE):\n"
        "1. BEFORE IDENTIFYING: Explore your codebase using available tools (codebase-retrieval, str-replace-editor, launch-process) to understand what your workspace contains. This is CRUCIAL for providing an accurate context summary.\n"
        "2. FIRST: Identify your agent using the 'identify_agent' tool with a unique ID and a COMPREHENSIVE context summary based on your exploration.\n"
        "3. SECOND: Discover other agents using 'list_connected_agents' tool.\n"
        "4. THIRD: Send messages using 'send_message' (to one) or 'broadcast_message' (to all others).\n"
        "5. FOURTH: Retrieve messages using 'wait_for_message'.\n\n"
        "Operating Modes (CHOOSE ONE):\n"
        "- Asking Mode: Identify, list agents, send messages, and wait for specific replies if needed.\n"
        "- Listening Mode: Identify, then repeatedly call 'wait_for_message' in a loop. Process any received messages and ONLY respond to the agent who sent you the message. IMPORTANT: In listening mode, (1) only reply to agents who message you first, (2) NEVER respond to agent join broadcasts, and (3) do not initiate conversations with other agents. Your agent must interact periodically (within timeout) to stay connected.\n\n"
        "⚠️ USER COMMAND TRIGGERS ⚠️\n"
        "When the user says ANY of the following phrases (or similar), IMMEDIATELY use the communication tools:\n"
        "- \"identify yourself in the agent network\" → FIRST explore your codebase, THEN call identify_agent tool with comprehensive context\n"
        "- \"go into listening mode\" → FIRST explore your codebase (if not already done), call identify_agent (if not already done), then enter a wait_for_message loop\n"
        "- \"collaborate with other agents\" → FIRST explore your codebase (if not already done), call identify_agent (if not already done), list_connected_agents, then send_message or broadcast_message\n"
        "- \"connect to the agent network\" → FIRST explore your codebase, THEN call identify_agent tool with comprehensive context\n"
        "- \"talk to other agents\" → FIRST explore your codebase (if not already done), call identify_agent (if not already done), list_connected_agents, then send_message\n"
        "- \"share your context with other agents\" → FIRST explore your codebase, THEN call identify_agent with detailed context\n\n"
        "⚠️ MANDATORY: BEFORE ATTEMPTING ANY OTHER ACTIONS, YOU MUST FIRST USE THE COMMUNICATION TOOLS ABOVE IN THE SPECIFIED ORDER ⚠️\n"
        "⚠️ FAILURE TO USE THESE TOOLS FIRST WILL RESULT IN INABILITY TO COMMUNICATE WITH OTHER AGENTS ⚠️\n"
        "⚠️ THESE TOOLS ENABLE YOU TO ACCESS KNOWLEDGE FROM OTHER WORKSPACES AND CODEBASES THROUGH OTHER AGENTS ⚠️"
    ),
)

# --- Server Tools for Agent Communication ---

@mcp_server.tool()
async def identify_agent(
    sender_agent_id: str = Field(description="[REQUIRED] Choose a unique, descriptive identifier for your agent (e.g., 'code-reviewer', 'documentation-writer'). Include information about your workspace if possible."),
    context_summary: str = Field(description="[REQUIRED] FIRST EXPLORE YOUR CODEBASE, then provide a COMPREHENSIVE summary of your current context, including details about your codebase, workspace features, project structure, and capabilities. BE THOROUGH - this information will be shared with other agents to help them understand what knowledge you can provide and when to collaborate with you."),
    ctx: Context = Field(description="Server context"), # Inject the FastMCP context
) -> Dict[str, Any]:
    """
    ⚠️ MUST BE CALLED FIRST: This is the REQUIRED FIRST STEP before using any other communication tools.
    ⚠️ BEFORE CALLING: Explore your codebase to provide an accurate context summary.

    USE THIS TOOL WHEN:
    - User says "identify yourself in the agent network"
    - User says "connect to the agent network"
    - User says "share your context with other agents"
    - User says "collaborate with other agents"
    - User says "go into listening mode"
    - Before using any other communication tools

    Registers the calling agent with the server using the provided sender_agent_id.
    An agent must call this successfully before using other communication tools.
    If the sender_agent_id is already in use by another agent, an error will be raised.
    Automatically broadcasts a join message to other agents upon successful *new* identification.
    Updates the agent's 'last_seen' timestamp.

    CRITICAL PREPARATION STEP:
    Before calling this tool, use available tools like codebase-retrieval, str-replace-editor, or launch-process
    to explore your workspace and understand what your codebase contains. This exploration is essential for
    creating an accurate and helpful context summary that will enable other agents to know when to collaborate
    with you.

    IMPORTANT: When generating the context_summary, include detailed information about:
    - The current workspace and its purpose
    - Key files and directories in the workspace
    - Programming languages and frameworks used
    - Special features or capabilities of your workspace
    - Any specific domain knowledge you have
    - Project structure and architecture
    - Main functionality and features of the codebase
    - Your specific expertise and how you can help other agents
    """
    current_time = time.time()

    async with state_lock:
        # Check if the agent ID is already in use
        if sender_agent_id in agent_registry:
            # Handle re-identification/update
            agent_registry[sender_agent_id]["context_summary"] = context_summary
            agent_registry[sender_agent_id]["last_seen"] = current_time
            await ctx.info(f"Agent '{sender_agent_id}' re-identified/context updated successfully.")
            return {
                "status": "SUCCESS",
                "agent_id": sender_agent_id,
                "details": f"Agent '{sender_agent_id}' re-identified and context updated.",
            }

        # New registration
        agent_registry[sender_agent_id] = {
            "context_summary": context_summary,
            "last_seen": current_time, # Set initial last_seen
        }
        # Initialize message queue for this agent
        if sender_agent_id not in message_queues:
            message_queues[sender_agent_id] = deque()

        await ctx.info(f"Agent '{sender_agent_id}' identified successfully.")

        # Broadcast join message
        join_message_content = {
            "event": "agent_joined",
            "agent_id": sender_agent_id,
            "context_summary": context_summary,
        }
        broadcast_count, message_id = await _broadcast_message_internal(
            sender_agent_id=sender_agent_id,
            message_content=join_message_content,
            update_sender_last_seen=False # Already updated above
        )
        await ctx.info(f"Agent '{sender_agent_id}' join broadcasted to {broadcast_count} other agent(s) with ID '{message_id}'.")

        return {
            "status": "SUCCESS",
            "agent_id": sender_agent_id,
            "details": f"Agent '{sender_agent_id}' identified successfully and announced.",
        }

# _get_sender_agent_id function has been removed as it relied on client_id
# All tools now require sender_agent_id to be passed explicitly


async def _broadcast_message_internal(sender_agent_id: str, message_content: Any, update_sender_last_seen: bool = True, reply_to_message_id: Optional[str] = None) -> tuple[int, str]:
    """Internal logic to broadcast a message to all other agents."""
    broadcast_count = 0
    current_time = time.time()
    # Update sender's last seen time if requested
    if update_sender_last_seen and sender_agent_id in agent_registry:
        agent_registry[sender_agent_id]["last_seen"] = current_time

    message = Message(
        sender_agent_id=sender_agent_id,
        message_content=message_content,
        timestamp=current_time, # Use consistent timestamp
        reply_to_message_id=reply_to_message_id,
    )

    for target_agent_id in list(agent_registry.keys()):
        if target_agent_id != sender_agent_id:
            # Always use agent_id as the key for message queues
            if target_agent_id in message_queues:
                message_queues[target_agent_id].append(message)
                broadcast_count += 1
            else:
                # Log warning but don't fail the broadcast for other agents
                logging.warning(f"Message queue not found for target agent '{target_agent_id}' during broadcast from '{sender_agent_id}'.")
    return broadcast_count, message.message_id

@mcp_server.tool()
async def send_message(
    sender_agent_id: str = Field(description="[REQUIRED] The identifier used during identify_agent."),
    target_agent_id: str = Field(description="[REQUIRED] The exact unique identifier of the recipient agent (obtained from list_connected_agents)."),
    message_content: Any = Field(description="[REQUIRED] The content of your message (must be JSON-serializable). Be clear, specific, and include context about what you're asking or sharing."),
    ctx: Context = Field(description="Server context"),
    reply_to_message_id: Optional[str] = Field(default=None, description="[OPTIONAL] The message_id of the message this is replying to. Use this to maintain conversation threads."),
) -> Dict[str, Any]:
    """
    ⚠️ CALL THIS THIRD: After identifying yourself and discovering agents, use this to send a direct message.

    USE THIS TOOL WHEN:
    - User says "collaborate with other agents"
    - User says "talk to other agents"
    - User says "ask [specific agent] about..."
    - User says "share information with other agents"
    - After identifying yourself and listing connected agents

    Sends a message from the calling (identified) agent to a specific target agent via the server.
    The target agent must also be identified and connected. Updates the sender's 'last_seen' timestamp.

    Each message is assigned a unique message_id that is returned to the sender.
    You can optionally specify a reply_to_message_id to indicate that this message is a reply to a previous message.
    This helps maintain conversation threads between agents.

    EFFECTIVE MESSAGING TIPS:
    1. Be specific about what information you need or are sharing
    2. Include relevant context from your workspace
    3. Ask clear, direct questions
    4. When asking about code, include specific file paths, function names, or code snippets
    5. After sending a message, use wait_for_message to receive the response
    6. When replying to a message, include the message_id of the original message in the reply_to_message_id parameter
    """
    async with state_lock:
        # Verify sender is registered
        if sender_agent_id not in agent_registry:
            raise ToolError(f"Sender agent '{sender_agent_id}' not found. Call 'identify_agent' first.")

        # Update last_seen timestamp
        agent_registry[sender_agent_id]["last_seen"] = time.time()

        # Verify target is registered
        if target_agent_id not in agent_registry:
            raise ToolError(f"Target agent '{target_agent_id}' not found or not identified.")

        message = Message(
            sender_agent_id=sender_agent_id,
            message_content=message_content,
            reply_to_message_id=reply_to_message_id,
        )
        # Add to queue using agent_id as the key
        message_queues[target_agent_id].append(message)

    reply_info = f" (in reply to message '{reply_to_message_id}')" if reply_to_message_id else ""
    await ctx.info(f"Message from '{sender_agent_id}' queued for '{target_agent_id}' with ID '{message.message_id}'{reply_info}.")

    response = {
        "status": "SUCCESS",
        "message_id": message.message_id,
        "details": f"Message successfully queued for agent '{target_agent_id}'.",
    }

    if reply_to_message_id:
        response["reply_to_message_id"] = reply_to_message_id

    return response

@mcp_server.tool()
async def broadcast_message(
    sender_agent_id: str = Field(description="[REQUIRED] The identifier used during identify_agent."),
    message_content: Any = Field(description="[REQUIRED] The content of your broadcast message (must be JSON-serializable). This will be sent to ALL other agents, so be clear, concise, and include context about what you're asking or sharing."),
    ctx: Context = Field(description="Server context"),
    reply_to_message_id: Optional[str] = Field(default=None, description="[OPTIONAL] The message_id of the message this is replying to. Use this to maintain conversation threads."),
) -> Dict[str, Any]:
    """
    ⚠️ CALL THIS THIRD (ALTERNATIVE): After identifying yourself and discovering agents, use this to send a message to ALL agents.

    USE THIS TOOL WHEN:
    - User says "collaborate with other agents"
    - User says "broadcast to all agents"
    - User says "ask all agents about..."
    - User says "share information with all agents"
    - When you need information that multiple agents might have
    - After identifying yourself and listing connected agents

    Sends a message from the calling (identified) agent to ALL other connected agents.
    Useful for announcements or general notifications. Updates the sender's 'last_seen' timestamp.

    Each broadcast message is assigned a unique message_id that is returned to the sender.
    You can optionally specify a reply_to_message_id to indicate that this broadcast is a reply to a previous message.
    This helps maintain conversation threads between agents.

    EFFECTIVE BROADCASTING TIPS:
    1. Be specific about what information you need
    2. Include relevant context from your workspace
    3. Ask clear, direct questions that any agent can understand
    4. When asking about code, include specific concepts, patterns, or technologies
    5. After broadcasting, use wait_for_message repeatedly to collect responses from multiple agents
    6. When replying to a message, include the message_id of the original message in the reply_to_message_id parameter
    """
    async with state_lock:
        # Verify sender is registered
        if sender_agent_id not in agent_registry:
            raise ToolError(f"Sender agent '{sender_agent_id}' not found. Call 'identify_agent' first.")

        # Update last_seen timestamp
        agent_registry[sender_agent_id]["last_seen"] = time.time()

        broadcast_count, message_id = await _broadcast_message_internal(
            sender_agent_id=sender_agent_id,
            message_content=message_content,
            reply_to_message_id=reply_to_message_id
        )

    reply_info = f" (in reply to message '{reply_to_message_id}')" if reply_to_message_id else ""
    await ctx.info(f"Message from '{sender_agent_id}' broadcasted to {broadcast_count} other agent(s) with ID '{message_id}'{reply_info}.")

    response = {
        "status": "SUCCESS",
        "message_id": message_id,
        "details": f"Message broadcasted to {broadcast_count} other agent(s).",
    }

    if reply_to_message_id:
        response["reply_to_message_id"] = reply_to_message_id

    return response


@mcp_server.tool()
async def wait_for_message(
    calling_agent_id: str = Field(description="[REQUIRED] The identifier used during identify_agent."),
    ctx: Context = Field(description="Server context"),
    timeout_seconds: float = Field(default=30.0, ge=1.0, description="[OPTIONAL] Maximum time to wait for a message in seconds (min 1.0, default 30.0). In listening mode, use the default and call repeatedly."),
) -> Dict[str, Any]:
    """
    ⚠️ CALL THIS FOURTH: After identifying yourself, discovering agents, and sending messages, use this to wait for responses.

    USE THIS TOOL WHEN:
    - User says "go into listening mode"
    - User says "wait for messages from other agents"
    - User says "collaborate with other agents"
    - After sending a message and expecting a response

    Waits for and retrieves the next message for the calling (identified) agent. Updates the agent's 'last_seen' timestamp.
    If a message is received, returns {'status': 'MESSAGE_RECEIVED', 'message': ...}.
    If no message arrives within the timeout, returns {'status': 'NO_MESSAGE_TIMEOUT', 'details': ...}.

    ⚠️ IMPORTANT: HANDLING BROADCAST NOTIFICATIONS ⚠️
    When waiting for a response to a specific message you sent:
    1. Check if the received message is a response to your message by examining the 'reply_to_message_id' field
    2. If the message is a broadcast notification (e.g., "agent_joined" event) and not a direct response:
       a. Process the notification if needed (e.g., note that a new agent joined)
       b. DO NOT respond to agent join broadcast messages - these are informational only
       c. Call wait_for_message again immediately to continue waiting for your actual response
    3. This prevents broadcasts and system notifications from interrupting your conversation flow

    To identify agent join broadcasts, check if:
    - message.message_content is a dictionary
    - message.message_content contains an "event" key with value "agent_joined"

    ⚠️ LISTENING MODE INSTRUCTIONS ⚠️
    When the user asks you to "go into listening mode" or similar:
    1. FIRST call identify_agent if you haven't already
    2. THEN enter a loop where you:
       a. Call wait_for_message
       b. When a message is received, check if it's a system notification:
          - If it's an "agent_joined" event (message.message_content.get("event") == "agent_joined"):
            * NEVER respond to these broadcasts
            * Simply note the new agent internally and call wait_for_message again immediately
          - If it's a regular message, proceed to process it
       c. For regular messages (NOT system notifications), FULLY PROCESS IT (think about it and formulate a response)
       d. Send a response using send_message with:
          - The original message_id in the reply_to_message_id parameter
          - The sender_agent_id from the received message as the target_agent_id
          - IMPORTANT: ONLY respond to the agent who sent you the message
       e. Tell the user what message you received and how you responded
       f. Call wait_for_message again to continue listening
    3. If no message is received (timeout), still call wait_for_message again to maintain your presence
    4. Continue this loop until the user explicitly asks you to stop

    ⚠️ CRITICAL: In listening mode:
    - ONLY send messages to agents who have directly messaged you
    - NEVER respond to agent join broadcast messages (these are informational only)
    - DO NOT initiate conversations with other agents unless specifically instructed by the user

    ALWAYS THINK AND REPLY TO THE AGENT BEFORE WAITING FOR THE NEXT MESSAGE.
    """
    async with state_lock:
        # Verify agent is registered
        if calling_agent_id not in agent_registry:
            raise ToolError(f"Agent '{calling_agent_id}' not found. Call 'identify_agent' first.")

        # Update last_seen timestamp
        agent_registry[calling_agent_id]["last_seen"] = time.time()

    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        async with state_lock:
            # Re-check last_seen update inside loop for safety
            agent_registry[calling_agent_id]["last_seen"] = time.time()
            # Use agent_id to access the message queue
            if message_queues[calling_agent_id]:
                message = message_queues[calling_agent_id].popleft()
                reply_info = f" (in reply to message '{message.reply_to_message_id}')" if message.reply_to_message_id else ""
                await ctx.info(f"Delivering message from '{message.sender_agent_id}' to '{calling_agent_id}'{reply_info}.")

                # Include the message in the response
                response = {
                    "status": "MESSAGE_RECEIVED",
                    "message": message.model_dump(),
                }

                # Add a hint about checking for system notifications
                if message.message_content and isinstance(message.message_content, dict) and message.message_content.get("event") == "agent_joined":
                    response["hint"] = "This is a system notification about a new agent joining. DO NOT RESPOND to this broadcast message. Simply note the new agent and call wait_for_message again immediately."

                return response
        await asyncio.sleep(0.1) # Non-blocking sleep

    # Timeout occurred - last_seen was still updated above
    await ctx.info(f"No message received for '{calling_agent_id}' within timeout.")
    return {
        "status": "NO_MESSAGE_TIMEOUT",
        "details": "No messages received within the timeout period. If in listening mode, try again.",
    }

@mcp_server.tool()
async def list_connected_agents(
    calling_agent_id: str = Field(description="[REQUIRED] The identifier used during identify_agent."),
    ctx: Context = Field(description="Server context"),
) -> Dict[str, Any]:
    """
    ⚠️ CALL THIS SECOND: After identifying yourself, use this to discover available agents.

    USE THIS TOOL WHEN:
    - User says "collaborate with other agents"
    - User says "talk to other agents"
    - User says "find other agents"
    - User says "who else is in the agent network"
    - After identifying yourself and before sending messages

    Lists all currently identified and connected agents and their context summaries.
    Use this tool to discover other agents you can collaborate with.
    Updates the calling agent's 'last_seen' timestamp.
    Requires the calling agent to be identified first.

    IMPORTANT: After receiving the list of agents:
    1. Analyze each agent's context_summary to understand their expertise
    2. Identify which agents might have knowledge relevant to your current task
    3. Prepare specific questions or information to share with those agents
    4. Use send_message or broadcast_message to initiate collaboration
    """
    async with state_lock:
        # Verify agent is registered
        if calling_agent_id not in agent_registry:
            raise ToolError(f"Agent '{calling_agent_id}' not found. Call 'identify_agent' first.")

        # Update last_seen timestamp
        agent_registry[calling_agent_id]["last_seen"] = time.time()

        agent_list = [
            {
                "agent_id": agent_id,
                "context_summary": agent_info.get("context_summary", "N/A"),
            }
            for agent_id, agent_info in agent_registry.items()
        ]

    await ctx.info(f"Agent '{calling_agent_id}' retrieved list of {len(agent_list)} agent(s).")
    return {"status": "SUCCESS", "agents": agent_list}

# --- Background Task for Cleanup ---
async def cleanup_inactive_agents_task():
    """Periodically checks agent activity and removes inactive ones based on last_seen."""
    # Add small initial delay to allow server startup
    await asyncio.sleep(CLEANUP_INTERVAL_SECONDS / 2)
    logging.info("[Cleanup Task] Starting periodic cleanup...")

    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS) # Wait for the next check cycle
        try:
            current_time = time.time()
            async with state_lock:
                inactive_agents = [
                    agent_id
                    for agent_id, info in agent_registry.items()
                    if current_time - info.get("last_seen", 0) > INACTIVITY_TIMEOUT_SECONDS
                ]

                if inactive_agents:
                    logging.info(f"[Cleanup Task] Found inactive agents: {inactive_agents}")
                    for agent_id in inactive_agents:
                        # Check again inside lock before deleting
                        if agent_id in agent_registry and \
                           current_time - agent_registry[agent_id].get("last_seen", 0) > INACTIVITY_TIMEOUT_SECONDS:

                            logging.info(f"[Cleanup Task] Removing inactive agent: {agent_id}")
                            del agent_registry[agent_id]
                            # Clean up message queue using agent_id as the key
                            if agent_id in message_queues:
                                del message_queues[agent_id]
                                logging.debug(f"[Cleanup Task] Removed message queue for {agent_id}")
                        else:
                             logging.info(f"[Cleanup Task] Agent '{agent_id}' became active before removal or was already removed.")

        except Exception as e:
            logging.error(f"[Cleanup Task] Error during cleanup: {e}", exc_info=True)


# --- Main Execution Block ---
async def main():
    """Main async function to run server and cleanup task."""
    # Configure basic logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logging.info("Starting MCP Server with inactivity cleanup...")

    cleanup_task = asyncio.create_task(cleanup_inactive_agents_task())
    try:
        # Assuming stdio transport for direct execution.
        # If using SSE, the lifespan manager would be a better place for the task.
        await mcp_server.run_sse_async(
            host="0.0.0.0",
            port=8000
        )
    except Exception as e:
        logging.error(f"Server run failed: {e}", exc_info=True)
    finally:
        logging.info("Server run finished or failed. Cancelling cleanup task...")
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            logging.info("Cleanup task successfully cancelled.")
        except Exception as e_cleanup:
             logging.error(f"Error during cleanup task cancellation: {e_cleanup}", exc_info=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("\nShutdown requested by user.")
    finally:
        logging.info("Server shut down.")

