import json
from typing import Sequence

from app.db.models import Message
from app.rag.bedrock_client import bedrock_chat

PROMPT = """You are an assistant for a law firm.
Read the following chat history between a user and an AI legal assistant.
Determine two things:
1. The primary language the user was speaking (e.g., "Arabic", "English").
2. A very short, 1-2 sentence summary of their legal problem, written in Arabic.

Return ONLY a JSON object with this exact structure:
{
  "chat_language": "string",
  "summary": "string"
}

Do not include markdown blocks or any other text.
"""

def summarize_escalation(messages: Sequence[Message]) -> dict:
    if not messages:
        return {"chat_language": "Unknown", "summary": "No messages."}
        
    chat_text = "\n".join(f"{m.role}: {m.content}" for m in messages)
    
    response = bedrock_chat(
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": f"<chat>\n{chat_text}\n</chat>"}
        ],
        response_format={"type": "json_object"}
    )
    
    raw = response.strip()
    if raw.startswith("```json"):
        raw = raw[7:]
    if raw.endswith("```"):
        raw = raw[:-3]
        
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        return {"chat_language": "Unknown", "summary": "Failed to generate summary."}
