import asyncio
import json
import os
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import threading
import queue
from typing import AsyncGenerator
from contextlib import asynccontextmanager

# Queue for MCP messages
message_queue = queue.Queue()

class MCPBridge:
    def __init__(self):
        self.process = None
        self.reader_thread = None
        self.initialized = False
        self.tools = []
        
    def start(self):
        """Start the MCP server process"""
        env = os.environ.copy()
        env['HUBSPOT_ACCESS_TOKEN'] = os.getenv('HUBSPOT_ACCESS_TOKEN', '')
        
        # Start the MCP server using the installed command
        self.process = subprocess.Popen(
            ['mcp-server-hubspot'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env
        )
        
        # Start reader thread
        self.reader_thread = threading.Thread(target=self._read_output)
        self.reader_thread.daemon = True
        self.reader_thread.start()
        
    def _read_output(self):
        """Read output from MCP server and queue messages"""
        while True:
            if self.process and self.process.stdout:
                line = self.process.stdout.readline()
                if line:
                    try:
                        # Parse MCP protocol messages
                        if line.strip():
                            message = json.loads(line.strip())
                            message_queue.put(message)
                            
                            # Store tools list when received
                            if message.get('method') == 'tools/list' or (message.get('result') and 'tools' in message.get('result', {})):
                                self.tools = message['result']['tools']
                    except Exception as e:
                        print(f"Error reading MCP output: {e}")
                        
    def send_request(self, request_data):
        """Send request to MCP server"""
        if self.process and self.process.stdin:
            try:
                json_line = json.dumps(request_data) + '\n'
                self.process.stdin.write(json_line)
                self.process.stdin.flush()
            except Exception as e:
                print(f"Error sending request: {e}")

    async def initialize(self):
        """Initialize MCP connection and get tools"""
        if not self.initialized:
            # Send initialize request
            self.send_request({
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {}
                    },
                    "clientInfo": {
                        "name": "n8n-mcp-client",
                        "version": "1.0.0"
                    }
                },
                "id": "init-1"
            })
            
            # Wait for initialization
            await asyncio.sleep(2)
            
            # Request tools list
            self.send_request({
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": {},
                "id": "tools-1"
            })
            
            # Wait for tools
            await asyncio.sleep(2)
            
            # Set default tools if none received
            if not self.tools:
                self.tools = [
                    {
                        "name": "hubspot_create_contact",
                        "description": "Create a new contact in HubSpot with duplicate prevention",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Contact name"},
                                "email": {"type": "string", "description": "Contact email"},
                                "properties": {"type": "object", "description": "Additional contact properties"}
                            },
                            "required": ["name"]
                        }
                    },
                    {
                        "name": "hubspot_create_company",
                        "description": "Create a new company in HubSpot with duplicate prevention",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Company name"},
                                "properties": {"type": "object", "description": "Additional company properties"}
                            },
                            "required": ["name"]
                        }
                    },
                    {
                        "name": "hubspot_get_company_activity",
                        "description": "Retrieve activity for specific companies",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "company_id": {"type": "string", "description": "HubSpot company ID"}
                            },
                            "required": ["company_id"]
                        }
                    },
                    {
                        "name": "hubspot_get_active_companies",
                        "description": "Retrieve most recently active companies",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "limit": {"type": "integer", "description": "Maximum number of companies to return", "default": 10}
                            }
                        }
                    },
                    {
                        "name": "hubspot_get_active_contacts",
                        "description": "Retrieve most recently active contacts",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "limit": {"type": "integer", "description": "Maximum number of contacts to return", "default": 10}
                            }
                        }
                    },
                    {
                        "name": "hubspot_get_recent_conversations",
                        "description": "Retrieve recent conversation threads with messages",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "limit": {"type": "integer", "description": "Maximum number of conversations to return", "default": 10}
                            }
                        }
                    },
                    {
                        "name": "hubspot_search_data",
                        "description": "Semantic search across previously retrieved HubSpot data",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Search query"}
                            },
                            "required": ["query"]
                        }
                    }
                ]
            
            self.initialized = True

# Initialize MCP bridge
mcp_bridge = MCPBridge()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    mcp_bridge.start()
    await asyncio.sleep(2)  # Give server time to initialize
    await mcp_bridge.initialize()
    yield
    # Shutdown
    if mcp_bridge.process:
        mcp_bridge.process.kill()

app = FastAPI(lifespan=lifespan)

# Enable CORS for n8n
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def event_generator() -> AsyncGenerator[str, None]:
    """Generate SSE events from MCP messages"""
    # Send initial connection with tools
    initial_message = {
        "jsonrpc": "2.0",
        "method": "initialized",
        "params": {
            "protocolVersion": "2024-11-05",
            "serverInfo": {
                "name": "hubspot-mcp-server",
                "version": "1.0.0"
            },
            "capabilities": {
                "tools": {}
            }
        }
    }
    yield f"data: {json.dumps(initial_message)}\n\n"
    
    # Send tools list
    if mcp_bridge.tools:
        tools_message = {
            "jsonrpc": "2.0",
            "result": {
                "tools": mcp_bridge.tools
            },
            "id": "tools-1"
        }
        yield f"data: {json.dumps(tools_message)}\n\n"
    
    while True:
        try:
            # Check for messages with timeout
            try:
                message = message_queue.get(timeout=0.1)
                yield f"data: {json.dumps(message)}\n\n"
            except queue.Empty:
                # Send keepalive
                yield ":keepalive\n\n"
                await asyncio.sleep(1)
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

@app.get("/sse")
async def sse_endpoint():
    """SSE endpoint for n8n integration"""
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable Nginx buffering
        }
    )

@app.post("/mcp/request")
async def mcp_request(request: Request):
    """Send request to MCP server"""
    try:
        data = await request.json()
        mcp_bridge.send_request(data)
        
        # Wait for response
        await asyncio.sleep(0.5)
        
        # Get latest message
        try:
            response = message_queue.get_nowait()
            return response
        except queue.Empty:
            return {"status": "processing"}
            
    except Exception as e:
        return {"error": str(e)}, 500

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy", 
        "mcp_running": mcp_bridge.process is not None,
        "initialized": mcp_bridge.initialized,
        "tools_count": len(mcp_bridge.tools)
    }

@app.get("/tools")
async def get_tools():
    """Get available tools"""
    # Ensure we have tools
    if not mcp_bridge.tools and mcp_bridge.initialized:
        await mcp_bridge.initialize()
    
    return {
        "tools": mcp_bridge.tools,
        "jsonrpc": "2.0",
        "id": "tools-list"
    }

# For n8n webhook integration
@app.post("/webhook/{action}")
async def n8n_webhook(action: str, request: Request):
    """Handle n8n webhook requests and convert to MCP calls"""
    try:
        data = await request.json()
        
        # Map n8n actions to MCP tool calls
        mcp_request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": f"hubspot_{action}",
                "arguments": data
            },
            "id": f"n8n-{action}-{asyncio.get_event_loop().time()}"
        }
        
        mcp_bridge.send_request(mcp_request)
        
        # Wait for response
        await asyncio.sleep(0.5)
        
        # Get latest message
        try:
            response = message_queue.get_nowait()
            return response
        except queue.Empty:
            return {"status": "processing"}
            
    except Exception as e:
        return {"error": str(e)}, 500

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
