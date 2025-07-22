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

app = FastAPI()

# Enable CORS for n8n
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Queue for MCP messages
message_queue = queue.Queue()

class MCPBridge:
    def __init__(self):
        self.process = None
        self.reader_thread = None
        
    def start(self):
        """Start the MCP server process"""
        env = os.environ.copy()
        env['HUBSPOT_ACCESS_TOKEN'] = os.getenv('HUBSPOT_ACCESS_TOKEN', '')
        
        # Start the MCP server using the Python module
        self.process = subprocess.Popen(
            ['python', '-m', 'mcp_server_hubspot'],
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
                            message_queue.put(line.strip())
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

# Initialize MCP bridge
mcp_bridge = MCPBridge()

@app.on_event("startup")
async def startup_event():
    """Start MCP server on app startup"""
    mcp_bridge.start()
    await asyncio.sleep(2)  # Give server time to initialize

async def event_generator() -> AsyncGenerator[str, None]:
    """Generate SSE events from MCP messages"""
    while True:
        try:
            # Check for messages with timeout
            try:
                message = message_queue.get(timeout=0.1)
                yield f"data: {message}\n\n"
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
        return {"status": "sent"}
    except Exception as e:
        return {"error": str(e)}, 500

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "mcp_running": mcp_bridge.process is not None}

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
        
        # Wait for response (simplified - in production use proper async handling)
        await asyncio.sleep(0.5)
        
        # Get latest message
        try:
            response = message_queue.get_nowait()
            return json.loads(response)
        except queue.Empty:
            return {"status": "processing"}
            
    except Exception as e:
        return {"error": str(e)}, 500

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
