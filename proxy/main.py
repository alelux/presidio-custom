import json
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
import requests

app = FastAPI()

PRESIDIO_ANALYZER = "http://presidio-analyzer:3000"
PRESIDIO_ANONYMIZER = "http://presidio-anonymizer:3000"
BEDROCK_GATEWAY = "http://bedrock-gateway:80"

# Mapping global par session (clé = hash du texte original)
session_mappings = {}

def detect_language(text: str) -> str:
    fr_words = ["je", "mon", "nom", "est", "bonjour", "merci", "le", "la", "les", "appelle"]
    it_words = ["mi", "mio", "sono", "chiamo", "ciao", "grazie", "il", "la", "di", "collega"]
    text_lower = text.lower()
    fr_score = sum(1 for w in fr_words if f" {w} " in f" {text_lower} ")
    it_score = sum(1 for w in it_words if f" {w} " in f" {text_lower} ")
    if it_score > fr_score:
        return "it"
    elif fr_score > 0:
        return "fr"
    return "en"

def analyze(text: str, language: str) -> list:
    try:
        r = requests.post(f"{PRESIDIO_ANALYZER}/analyze",
            json={"text": text, "language": language}, timeout=10)
        return r.json() if r.status_code == 200 else []
    except:
        return []

def build_mapping(text: str, results: list) -> dict:
    mapping = {}
    sorted_results = sorted(results, key=lambda x: x["start"])
    merged = []
    for result in sorted_results:
        if merged and result["entity_type"] == merged[-1]["entity_type"] \
                and result["start"] - merged[-1]["end"] <= 2:
            merged[-1]["end"] = result["end"]
        else:
            merged.append(dict(result))
    for result in merged:
        original = text[result["start"]:result["end"]]
        placeholder = f"<{result['entity_type']}_{abs(hash(original)) % 10000}>"
        mapping[placeholder] = original
    return mapping

def anonymize_text(text: str, results: list, mapping: dict) -> str:
    if not results:
        return text
    try:
        anonymizers = {
            placeholder: {"type": "replace", "new_value": placeholder}
            for placeholder, original in mapping.items()
            for r in results
            if text[r["start"]:r["end"]] == original
        }
        # Simplification : un anonymizer par entity_type
        anon_by_type = {}
        for result in results:
            original = text[result["start"]:result["end"]]
            for ph, orig in mapping.items():
                if orig == original:
                    anon_by_type[result["entity_type"]] = {"type": "replace", "new_value": ph}

        r = requests.post(f"{PRESIDIO_ANONYMIZER}/anonymize",
            json={"text": text, "anonymizers": anon_by_type, "analyzer_results": results},
            timeout=10)
        return r.json()["text"] if r.status_code == 200 else text
    except:
        return text

def deanonymize(text: str, mapping: dict) -> str:
    for placeholder, original in mapping.items():
        text = text.replace(placeholder, original)
    return text

def process_messages(messages: list, mapping: dict) -> tuple[list, dict]:
    """Anonymise tous les messages user et retourne le mapping mis à jour"""
    processed = []
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            text = msg["content"]
            language = detect_language(text)
            results = analyze(text, language)
            if results:
                new_mapping = build_mapping(text, results)
                mapping.update(new_mapping)
                text = anonymize_text(text, results, new_mapping)
            processed.append({**msg, "content": text})
        else:
            # Désanonymiser les messages assistant existants
            content = msg.get("content", "")
            if isinstance(content, str):
                content = deanonymize(content, mapping)
            processed.append({**msg, "content": content})
    return processed, mapping

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy(request: Request, path: str):
    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)

    mapping = {}

    # Intercepter uniquement les chat completions
    if "chat/completions" in path and request.method == "POST":
        try:
            data = json.loads(body)
            messages = data.get("messages", [])
            messages, mapping = process_messages(messages, mapping)
            data["messages"] = messages
            body = json.dumps(data).encode()
            headers["content-length"] = str(len(body))
        except Exception as e:
            print(f"[PROXY] Erreur anonymisation: {e}")

    target_url = f"{BEDROCK_GATEWAY}/{path}"
    is_streaming = False

    try:
        req_data = json.loads(body) if body else {}
        is_streaming = req_data.get("stream", False)
    except:
        pass

    if is_streaming:
        async def stream_response():
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    request.method,
                    target_url,
                    headers=headers,
                    content=body,
                    params=dict(request.query_params)
                ) as response:
                    async for chunk in response.aiter_text():
                        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                            try:
                                json_str = chunk[6:].strip()
                                chunk_data = json.loads(json_str)
                                for choice in chunk_data.get("choices", []):
                                    delta = choice.get("delta", {})
                                    if "content" in delta and delta["content"]:
                                        delta["content"] = deanonymize(delta["content"], mapping)
                                yield f"data: {json.dumps(chunk_data)}\n\n"
                            except:
                                yield chunk
                        else:
                            yield chunk

        return StreamingResponse(stream_response(), media_type="text/event-stream")
    else:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.request(
                request.method,
                target_url,
                headers=headers,
                content=body,
                params=dict(request.query_params)
            )
            try:
                resp_data = response.json()
                for choice in resp_data.get("choices", []):
                    if "message" in choice and "content" in choice["message"]:
                        choice["message"]["content"] = deanonymize(
                            choice["message"]["content"], mapping
                        )
                return Response(
                    content=json.dumps(resp_data),
                    status_code=response.status_code,
                    headers=dict(response.headers)
                )
            except:
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    headers=dict(response.headers)
                )
