import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import openai


def shell_tool(cmd: str) -> Dict[str, Any]:
    try:
        result = subprocess.run(
            ["docker", "exec",
             # "-w", str(Path.cwd()),
             "sandbox", "sh", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Command timed out after 30 seconds",
        }
    except OSError as exc:
        return {
            "success": False,
            "error": str(exc),
        }


shell_tool.tool_signature = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": "Execute shell commands in a Docker container (sandbox) using sh and return structured results.",
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {
                    "type": "string",
                    "description": "Shell command to execute in the Docker container",
                }
            },
            "required": ["cmd"],
        },
    },
}


TOOL_INDEX = {
    "shell": shell_tool,
}

TOOL_SIGNATURES = [shell_tool.tool_signature]


class OAI:
    def __init__(self, endpoints: Dict[str, Dict[str, Any]],
                 max_tool_iterations: int = 15):
        self.endpoints = endpoints
        self.model = ''
        self.stream = True
        self.client: openai.OpenAI | None = None
        self.max_tool_iterations = max_tool_iterations

    def use_endpoint(self, endpoint: str, model: str, stream: bool) -> None:
        self.model = model
        self.stream = stream
        endpoint_config = self.endpoints[endpoint]
        api_key = endpoint_config['key_env']
        if api_key.isupper():
            env_value = os.getenv(api_key)
            if not env_value:
                raise ValueError(f"Missing {api_key} environment variable")
            api_key = env_value
        self.client = openai.OpenAI(api_key=api_key, base_url=endpoint_config['url'])

    def chat(self, messages: List[Dict[str, Any]], use_tools: bool = True) -> Any:
        """Returns an iterator over content chunks."""
        tools = TOOL_SIGNATURES if use_tools else None
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            stream=self.stream,
            temperature=0.7,
            extra_body={"options": {"num_ctx": 256 * 1024}},
        )
        if not self.stream:
            # Non-streaming: return iterator over single result
            content = response.choices[0].message.model_dump(exclude_none=True).get('content', '') or ''
            return iter([content])
        # Streaming: return generator over chunks
        return self.stream_chat(response)

    def stream_chat(self, stream: Any):
        """Generator that yields content chunks from streaming response."""
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    def get_tool_calls(self, message: Dict[str, Any]) -> List[Dict[str, Any]]:
        return message.get('tool_calls') or []

    def message_to_dict(self, message: Dict[str, Any]) -> Dict[str, Any]:
        return message

    def execute_tool_call(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        function_name = tool_call['function']['name']
        arguments = json.loads(tool_call['function']['arguments'])
        print(f"DEBUG: Executing tool: {function_name} with args: {str(arguments)[:40]}")
        if function_name not in TOOL_INDEX:
            print(f"DEBUG: Tool not found in index: {function_name}")
            return {"error": f"Unknown tool: {function_name}", "success": False}
        try:
            result = TOOL_INDEX[function_name](**arguments)
            print(f"DEBUG: Tool execution result: {repr(result)[:40]}...")
            return {"result": result, "success": True}
        except Exception as exc:
            print(f"DEBUG: Tool execution error: {exc}")
            return {"error": str(exc), "success": False}

    def process_turn(self, messages: List[Dict[str, Any]], persist: Callable[[Any, str], None]):
        """Generator that yields content chunks, handling tool calls internally."""
        iteration = 0
        while iteration < self.max_tool_iterations:
            iteration += 1
            print(f"DEBUG: Tool call iteration {iteration}")
            
            # Get response as iterator
            content_iter = self.chat(messages)
            
            # Collect content and check for tool calls
            chunks = []
            response = None
            for chunk in content_iter:
                chunks.append(chunk)
            
            # For non-streaming, we need to get the full response for tool calls
            if not self.stream:
                # Re-call to get the response dict with tool_calls
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=TOOL_SIGNATURES,
                    tool_choice="auto",
                    stream=False,
                    temperature=0.7,
                    extra_body={"options": {"num_ctx": 256 * 1024}},
                )
                response = response.choices[0].message.model_dump(exclude_none=True)
                tool_calls = self.get_tool_calls(response)
                
                # Yield the content
                full_content = response.get('content') or ''
                if full_content:
                    yield full_content
                    
                if not tool_calls:
                    print("DEBUG: AI did not make any tool calls")
                    break
                    
                print(f"DEBUG: AI made {len(tool_calls)} tool calls")
                # Handle tool calls (existing logic)
                assistant_msg = response
                persist([assistant_msg], 'asst')
                tool_results = []
                for tool_call in tool_calls:
                    result = self.execute_tool_call(tool_call)
                    tool_results.append({
                        'role': 'tool',
                        'tool_call_id': tool_call['id'],
                        'name': tool_call['function']['name'],
                        'content': json.dumps(result),
                        'timestamp': __import__('datetime').datetime.now().isoformat() + 'Z',
                    })
                persist(tool_results, 'tool')
                messages.append(assistant_msg)
                messages.extend(tool_results)
            else:
                # Streaming mode - no tool call support for now
                # Just yield all chunks
                for chunk in chunks:
                    yield chunk
                print("DEBUG: Streaming mode, no tool calls")
                break
        else:
            yield "Error: Too many tool call iterations"

    def process_tool_calls(
        self,
        response: Any,
        messages: List[Dict[str, Any]],
        persist: Callable[[Any, str], None],
    ) -> str:
        iteration = 0
        while iteration < self.max_tool_iterations:
            iteration += 1
            print(f"DEBUG: Tool call iteration {iteration}")
            tool_calls = self.get_tool_calls(response)
            if not tool_calls:
                ai_response = response.get('content') or ''
                break
            print(f"DEBUG: Processing {len(tool_calls)} tool calls")
            assistant_tool_call_msg = self.message_to_dict(response)
            persist([assistant_tool_call_msg], 'asst')
            tool_results = []
            for tool_call in tool_calls:
                result = self.execute_tool_call(tool_call)
                tool_results.append({
                    'role': 'tool',
                    'tool_call_id': tool_call['id'],
                    'name': tool_call['function']['name'],
                    'content': json.dumps(result),
                    'timestamp': __import__('datetime').datetime.now().isoformat() + 'Z',
                })
            persist(tool_results, 'tool')
            print(f"DEBUG: Tool results to send to AI: {len(tool_results)}")
            messages.append(assistant_tool_call_msg)
            messages.extend(tool_results)
            try:
                print("DEBUG: Sending tool results to AI for next response")
                response = self.chat(messages)
                next_tool_calls = self.get_tool_calls(response)
                if next_tool_calls:
                    print("DEBUG: AI wants to make more tool calls:", len(next_tool_calls))
                    continue
                print("DEBUG: AI is done with tool calls, providing final response")
                ai_response = response.get('content') or ''
                break
            except Exception as exc:
                print(f"DEBUG: Error getting AI response: {exc}")
                ai_response = f"Error after tool execution: {str(exc)}"
                break
        else:
            ai_response = "Error: Too many tool call iterations, possible infinite loop"
        print(f"DEBUG: Final AI response after {iteration} iterations: {ai_response[:200]}...")
        return ai_response
