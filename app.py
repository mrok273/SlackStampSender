import os
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from notion_client import Client
import asyncio
import logging  # new import
from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("app")

# Environment vars / constants
SLACK_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "C08CB5CG1B7")  # Slack channel to monitor
ALLOWED_USER = os.getenv("ALLOWED_USER_ID", "U123456")        # Slack user who posts the article message

# NEW: Slack signing secret for Bolt integration
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DB_ID = os.getenv("NOTION_DATABASE_ID")
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

app = FastAPI()


def get_notion_client():
    
    return Client(auth=NOTION_TOKEN)

async def fetch_slack_message(channel: str, ts: str, thread_ts: str = None) -> str:
    logger.debug("Fetching message for channel %s at ts %s, thread_ts: %s", channel, ts, thread_ts)
    headers = {"Authorization": f"Bearer {SLACK_TOKEN}"}
    async with httpx.AsyncClient() as client:
# If this is a threaded reply, use conversations.replies API to get all messages in the thread:
        url = "https://slack.com/api/conversations.replies"
        params = {"channel": channel, "ts": ts}
        response = await client.get(url, params=params, headers=headers)
        data = response.json()
        if not data.get("ok") or "messages" not in data:
            logger.error("Slack API error in conversations.replies: %s", data)
            return None
        # Pick the message with the matching ts:
        for msg in data["messages"]:
            if msg.get("ts") == ts:
                message_text = msg.get("text")
                logger.debug("Fetched threaded slack message text: %s", message_text)
                return message_text
        logger.error("Message with ts %s not found in thread %s replies", ts, thread_ts)
        return None


def parse_article_from_message(text: str):
    """
    Parses the Slack message text.
    Expected format:
       *{article['Title']}*
       <{article['Url']}>
       {article['Summary']}
    """
    lines = text.splitlines()
    if len(lines) < 2:
        logger.debug("Insufficient lines in message: %s", text)
        return None
    # Extract title (remove asterisks)
    title = lines[0].strip().strip("*")
    # Extract URL from the line surrounded by <>
    url_line = lines[1].strip()
    url = url_line[1:-1] if url_line.startswith("<") and url_line.endswith(">") else url_line
    summary = "\n".join(lines[2:]) if len(lines) > 2 else ""
    article = {"title": title, "url": url, "summary": summary}
    logger.debug("Parsed article: %s", article)
    return article


async def find_notion_page_by_url(article_url: str):
    def query_notion():
        notion = get_notion_client()
        response = notion.databases.query(
            database_id=NOTION_DB_ID,
            filter={"property": "URL", "url": {"equals": article_url}}
        )
        results = response.get("results", [])
        logger.debug("Notion query results for URL %s: %s", article_url, results)
        return results[0]["id"] if results else None
    page_id = await asyncio.to_thread(query_notion)
    logger.debug("Found Notion page ID: %s for URL: %s", page_id, article_url)
    return page_id


async def update_notion_score(page_id: str, score: int):
    def update_score():
        notion = get_notion_client()
        result = notion.pages.update(
            page_id,
            properties={
                "my_score": {
                    "number": score
                }
            }
        )
        logger.debug("Notion update response for page %s: %s", page_id, result)
        return result
    try:
        result = await asyncio.to_thread(update_score)
        logger.debug("Successfully updated Notion page %s with score %s", page_id, score)
        return result
    except Exception as e:
        logger.error("Error updating Notion page %s: %s", page_id, e)
        raise


@app.post("/slack/events")
async def slack_events(request: Request):
    data = await request.json()
    logger.debug("Received Slack event data: %s", data)

    if "challenge" in data:
        return JSONResponse({"challenge": data["challenge"]})

    event = data.get("event", {})
    if event.get("type") == "reaction_added":
        channel = event.get("item", {}).get("channel")
        logger.debug("Processing reaction event from channel: %s", channel)
        if channel != SLACK_CHANNEL:
            logger.debug("Channel %s not monitored. Ignoring.", channel)
            return JSONResponse({"message": "Channel not monitored"}, status_code=200)
        reaction = event.get("reaction")
        logger.debug("Reaction received: %s", reaction)
        if reaction not in ("+1", "thumbsup","o","thumbsdown","-1","x"):
            logger.debug("Reaction %s not processed", reaction)
            return JSONResponse({"message": "Reaction not processed"}, status_code=200)
        
        if reaction in ("+1", "thumbsup","o"):
            score=1
        else: #reaction in ("-1", "thumbsdown","x"):
            score=0

        item = event.get("item", {})
        ts = item.get("ts")
        thread_ts = item.get("thread_ts")  # may be None if not a thread reply

        message_text = await fetch_slack_message(channel, ts, thread_ts)
        if not message_text:
            logger.error("Failed to fetch message for ts: %s", ts)
            return JSONResponse({"message": "Message fetch failed"}, status_code=200)

        article = parse_article_from_message(message_text)
        if not article:
            logger.error("Article parsing failed for message: %s", message_text)
            return JSONResponse({"message": "Article parse failed"}, status_code=200)

        notion_page_id = await find_notion_page_by_url(article["url"])
        if not notion_page_id:
            logger.error("Notion page not found for URL: %s", article["url"])
            return JSONResponse({"message": "Notion page not found"}, status_code=200)

        update_resp = await update_notion_score(notion_page_id, score)
        logger.debug("Notion update completed with response: %s", update_resp)
        return JSONResponse({"message": "Notion updated", "response": update_resp}, status_code=200)

    logger.debug("Unhandled event type.")
    return JSONResponse({"message": "Event type not handled"}, status_code=200)


# NEW: Slack Bolt integration for testing with slack_bolt (有効な SLACK_SIGNING_SECRET がある場合のみ)
if SLACK_SIGNING_SECRET:
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.adapter.fastapi import SlackRequestHandler

    bolt_app = AsyncApp(token=SLACK_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
    
    @bolt_app.event("reaction_added")
    async def bolt_reaction_added(body, logger):
        logger.debug("Bolt event received: %s", body)
        event = body.get("event", {})
        channel = event.get("item", {}).get("channel")
        logger.debug("Bolt event channel: %s", channel)
        logger.debug("SLACK_CHANNEL: %s", SLACK_CHANNEL)
        if channel != SLACK_CHANNEL:
            logger.info("Channel not monitored: %s", channel)
            return
        reaction = event.get("reaction")
        logger.debug("Bolt event reaction: %s", reaction)
        if reaction not in ("thumbsup", "thumbsdown"):
            logger.info("Reaction not processed: %s", reaction)
            return
        score = 1 if reaction == "thumbsup" else 0

        item = event.get("item", {})
        ts = item.get("ts")
        thread_ts = item.get("thread_ts")  # Check for threaded reply
        message_text = await fetch_slack_message(channel, ts, thread_ts)
        if not message_text:
            logger.info("Message fetch failed in bolt event")
            return
        article = parse_article_from_message(message_text)
        if not article:
            logger.info("Article parse failed in bolt event")
            return
        notion_page_id = await find_notion_page_by_url(article["url"])
        if not notion_page_id:
            logger.info("Notion page not found in bolt event")
            return
        update_resp = await update_notion_score(notion_page_id, score)
        logger.info("Notion updated in bolt event: %s", update_resp)
    
    bolt_handler = SlackRequestHandler(bolt_app)

    @app.post("/slack/bolt_events")
    async def bolt_events(request: Request):
        return await bolt_handler.handle(request)


# NEW: Run server with uvicorn and expose via ngrok for debugging
if __name__ == "__main__":
    import uvicorn
    from pyngrok import ngrok

    port = 6666
    public_url = ngrok.connect(port, bind_tls=True)
    print("ngrok tunnel public URL:", public_url)
    uvicorn.run(app, host="0.0.0.0", port=port)
