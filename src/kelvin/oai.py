import copy
import datetime
import json
import os
import subprocess
from typing import Any, Callable, Dict, Iterator, List

import openai


def shell_tool(cmd: str) -> Dict[str, Any]:
    try:
        result = subprocess.run(
            ["docker", "exec", "sandbox", "sh", "-c", cmd],
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
    def __init__(self, endpoints: Dict[str, Dict[str, Any]], max_tool_iterations: int = 15):
        self.endpoints = endpoints
        self.model = ""
        self.stream = True
        self.client: openai.OpenAI | None = None
        self.max_tool_iterations = max_tool_iterations

    def use_endpoint(self, endpoint: str, model: str, stream: bool) -> None:
        self.model = model
        self.stream = stream
        endpoint_config = self.endpoints[endpoint]
        api_key = endpoint_config["key_env"]
        if api_key.isupper():
            env_value = os.getenv(api_key)
            if not env_value:
                raise ValueError(f"Missing {api_key} environment variable")
            api_key = env_value
        self.client = openai.OpenAI(api_key=api_key, base_url=endpoint_config["url"])

    def completion_kwargs(self, messages: List[Dict[str, Any]], *, stream: bool, use_tools: bool) -> Dict[str, Any]:
        return {
            "model": self.model,
            "messages": messages,
            "tools": TOOL_SIGNATURES if use_tools else None,
            "tool_choice": "auto",
            "stream": stream,
            "temperature": 0.7,
            "extra_body": {"options": {"num_ctx": 256 * 1024}},
        }

    def empty_message(self) -> Dict[str, Any]:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [],
            "done": False,
        }

    def normalize_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self.empty_message()
        normalized["role"] = message.get("role") or "assistant"
        normalized["content"] = message.get("content") or ""
        normalized["tool_calls"] = copy.deepcopy(message.get("tool_calls") or [])
        return normalized

    def ensure_tool_call(self, tool_calls: List[Dict[str, Any]], idx: int) -> Dict[str, Any]:
        while len(tool_calls) <= idx:
            tool_calls.append(
                {
                    "id": "",
                    "type": "function",
                    "function": {
                        "name": "",
                        "arguments": "",
                    },
                }
            )
        return tool_calls[idx]

    def empty_tool_call(self) -> Dict[str, Any]:
        return {"id":"",
                "type":"function",
                "function":{"name":"",
                            "arguments": ""}}

    def mk_itr(self, messages: List[Dict[str, Any]], stream: bool, use_tools: bool = True) -> Iterator[Dict[str, Any]]:
        response = self.client.chat.completions.create(
            **self.completion_kwargs(messages, stream=stream, use_tools=use_tools)
        )
        if not stream:
            message = response.choices[0].message.model_dump(exclude_none=True)
            message = self.normalize_message(message)
            message["done"] = True
            yield message
            return

        contents = []
        tools_dict = {}
        
        for chunk in response:
            if not chunk.choices:
                continue
            else:
                pass # do nothing

            delta = chunk.choices[0].delta

            if content := delta.content:
                message = self.empty_message()
                message["content"] = content
                contents.append(content)
                yield message

            for tc_delta in (delta.tool_calls or []):
                index = tc_delta.index
                if index is None:
                    raise ValueError("tool_call delta missing index")
                if tool_call := tools_dict.get(index):
                    pass # do nothing
                else:
                    tool_call = tools_dict[index] = self.empty_tool_call()
                    pass
                if tc_delta.type:
                    tool_call["type"] = tc_delta.type
                if tc_delta.id:
                    tool_call["id"] += tc_delta.id
                if func := tc_delta.function:
                    if func.name:
                        tool_call["function"]["name"] += func.name
                    if func.arguments:
                        tool_call["function"]["arguments"] += func.arguments
            
        try:
            tool_calls = [ tools_dict[i] for i in range(len(tools_dict)) ]
        except KeyError as e:
            raise ValueError(f"missing index in tool_calls: {e.args[0]}")
        
        message = self.empty_message()
        message["content"] = ''.join(contents)
        message["tool_calls"] = tool_calls            
        message["done"] = True
        yield message
        
    def execute_tool_call(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        function_name = tool_call["function"]["name"]
        if function_name not in TOOL_INDEX:
            return {"error": f"Unknown tool: {function_name}", "success": False}

        try:
            raw_arguments = tool_call["function"].get("arguments") or "{}"
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            return {
                "error": f"Invalid JSON arguments for tool {function_name}: {exc}",
                "success": False,
            }

        try:
            result = TOOL_INDEX[function_name](**arguments)
            return {"result": result, "success": True}
        except Exception as exc:
            return {"error": str(exc), "success": False}

    def tool_results_for(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        results = []
        for tool_call in tool_calls:
            result = self.execute_tool_call(tool_call)
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": tool_call["function"]["name"],
                    "content": json.dumps(result),
                    "timestamp": datetime.datetime.now().isoformat() + "Z",
                }
            )
        return results

    def process_turn(self, messages: List[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
        for round_num in range(self.max_tool_iterations):
            for seq, item in enumerate(self.mk_itr(messages, self.stream)):
                event = {
                    "round": round_num + 1,
                    "seq": seq + 1,
                    "role": item["role"],
                    "content": item["content"],
                    "tool_calls": copy.deepcopy(item["tool_calls"]),
                    "done": item["done"],
                }
                yield event
                pass

            if not (tool_calls := item["tool_calls"]):
                return
            
            tool_results = self.tool_results_for(tool_calls)
            for offset, tool_result in enumerate(tool_results):
                yield {
                    "round": round_num + 1,
                    "seq": seq + 1 + offset + 1,
                    "role": "tool",
                    "content": tool_result["content"],
                    "tool_calls": [],
                    "done": True,
                    "tool_call_id": tool_result["tool_call_id"],
                    "name": tool_result["name"],
                    "timestamp": tool_result["timestamp"],
                }
                pass # do nothing
            messages.append(self.normalize_message(item))
            messages.extend(tool_results)
            pass # do nothing
        
        yield {
            "round": round_num + 1,
            "seq": 1,
            "role": "error",
            "content": "Too many tool call iterations",
            "tool_calls": [],
            "done": True,
        }
