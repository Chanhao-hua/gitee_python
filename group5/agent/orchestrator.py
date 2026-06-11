"""Natural-language Agent orchestrator with tool-calling fallback."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from agent.skills import TOOL_SCHEMAS, execute_skill


class AgentOrchestrator:
    """Routes natural language commands to whitelisted local skills."""

    def __init__(self) -> None:
        self.api_key = os.getenv("GITEE_AI_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("GITEE_AI_BASE_URL", "https://ai.gitee.com/v1")
        self.model = os.getenv("GITEE_AI_MODEL", "qwen2.5-72b-instruct")

    def handle_command(self, command: str) -> dict[str, Any]:
        command = command.strip()
        if not command:
            return {"ok": False, "error": "指令不能为空。"}

        if self.api_key:
            llm_result = self._try_llm_tool_call(command)
            if llm_result.get("ok"):
                return llm_result

        parsed = self._fallback_parse(command)
        skill_result = execute_skill(parsed["action"], parsed.get("args", {}))
        return {
            "ok": skill_result.get("ok", False),
            "routing": "fallback_json_action",
            "parsed_action": parsed,
            "result": skill_result,
            "message": self._summarize(skill_result),
        }

    def _try_llm_tool_call(self, command: str) -> dict[str, Any]:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是数码电商数据采集项目的 Agent Orchestrator。"
                            "只能通过提供的 tools 调用本地 skill，不要编造执行结果。"
                            "京东/苏宁采价格、商家、评论数量和评价标签，不采集评论正文；"
                            "ZOL 只提供排行参考。"
                        ),
                    },
                    {"role": "user", "content": command},
                ],
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
                temperature=0,
            )
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            if not tool_calls:
                parsed = self._extract_json_action(getattr(message, "content", "") or command)
                result = execute_skill(parsed["action"], parsed.get("args", {}))
                return {
                    "ok": result.get("ok", False),
                    "routing": "llm_json_action",
                    "parsed_action": parsed,
                    "result": result,
                    "message": self._summarize(result),
                }

            results = []
            for call in tool_calls:
                name = call.function.name
                args = json.loads(call.function.arguments or "{}")
                results.append(execute_skill(name, args))
            return {
                "ok": all(item.get("ok") for item in results),
                "routing": "llm_tool_calls",
                "result": results[0] if len(results) == 1 else results,
                "message": "Agent 已通过 tool calling 调用本地 skill。",
            }
        except Exception as exc:
            return {"ok": False, "routing": "llm_failed", "error": str(exc)}

    def _fallback_parse(self, command: str) -> dict[str, Any]:
        source = _extract_sources(command)
        keyword = _extract_keyword(command)
        interval = _extract_interval_minutes(command)

        if any(word in command for word in ["状态", "任务", "job", "Job"]):
            return {"action": "job_status", "args": {}}
        if interval and any(word in command for word in ["定时", "每", "周期", "schedule"]):
            return {
                "action": "schedule_crawl",
                "args": {
                    "source": source,
                    "keyword": keyword,
                    "interval_minutes": interval,
                    "limit": 30,
                    "mode": "live",
                },
            }
        if any(word in command for word in ["抓取", "爬取", "更新", "crawl"]):
            return {
                "action": "run_crawl",
                "args": {"source": source, "keyword": keyword, "limit": 30, "mode": "live"},
            }
        if any(word in command for word in ["对比", "比较", "compare"]):
            return {"action": "compare_sources", "args": {"keyword": keyword}}
        return {
            "action": "query_products",
            "args": {"source": source, "keyword": keyword, "limit": 100},
        }

    def _extract_json_action(self, text: str) -> dict[str, Any]:
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "action" in data:
                return data
        except json.JSONDecodeError:
            pass
        return self._fallback_parse(text)

    def _summarize(self, result: dict[str, Any]) -> str:
        action = result.get("action")
        if action == "schedule_crawl":
            return f"已创建 {len(result.get('jobs', []))} 个定时爬虫任务。"
        if action == "run_crawl":
            return f"已写入 {result.get('inserted', 0)} 条爬虫结果。"
        if action == "query_products":
            return f"查询到 {result.get('count', 0)} 条商品数据。"
        if action == "compare_sources":
            return "已生成京东、苏宁易购、中关村在线综合对比。"
        if action == "job_status":
            return f"当前共有 {len(result.get('jobs', []))} 个定时任务。"
        return "Agent skill 已执行。"


def _extract_sources(command: str) -> str:
    sources = []
    if any(word in command for word in ["京东", "jd", "JD"]):
        sources.append("jd")
    if any(word in command for word in ["苏宁", "suning"]):
        sources.append("suning")
    if any(word in command for word in ["中关村", "ZOL", "zol"]):
        sources.append("zol")
    return ",".join(sources) if sources else "all"


def _extract_keyword(command: str) -> str:
    patterns = [
        r"iPhone\s*16(?:\s*Pro)?",
        r"iPhone\s*15(?:\s*Pro)?",
        r"小米\s*15",
        r"华为\s*Mate\s*\d*",
        r"荣耀\s*Magic\d*",
        r"OPPO\s*Find\s*\w*",
        r"vivo\s*X\d*",
        r"手机",
    ]
    for pattern in patterns:
        match = re.search(pattern, command, flags=re.IGNORECASE)
        if match:
            return " ".join(match.group(0).split())

    quoted = re.search(r"[“\"']([^”\"']+)[”\"']", command)
    if quoted:
        return quoted.group(1).strip()
    return "手机"


def _extract_interval_minutes(command: str) -> int | None:
    minute_match = re.search(r"每\s*(\d+)\s*分钟", command)
    if minute_match:
        return int(minute_match.group(1))
    hour_match = re.search(r"每\s*(\d+)\s*(?:小时|个小时)", command)
    if hour_match:
        return int(hour_match.group(1)) * 60
    return None
