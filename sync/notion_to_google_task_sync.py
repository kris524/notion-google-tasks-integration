"""Script that handles the sync from to Notion to Google tasks"""

import os
from notion_client import Client
from pprint import pprint
from dotenv import load_dotenv
from typing import List, Dict
import os.path
import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import redis

# logging.basicConfig(level=logging.DEBUG)

# -----------------
# SETUP

SCOPES = ["https://www.googleapis.com/auth/tasks"]

r = redis.Redis(host="localhost", port=6379, decode_responses=True, db=0)

r_reverse = redis.Redis(host="localhost", port=6379, decode_responses=True, db=1)

# -----------------
# NOTION FUNCTIONS


def get_all_pages(client: Client) -> List[str]:
    """Get all pages that have enabled the integration"""
    # import ipdb;ipdb.set_trace()
    page_ids = []
    for page in client.search()["results"]:
        page_ids.append(page["id"])

    if not page_ids:
        logging.info("No notion pages have enabled the integration")

    return page_ids


def get_all_blocks(client: Client, page_id: str) -> List[Dict[str, str]]:
    """Get all blocks on the page given via page_id"""
    response = client.blocks.children.list(block_id=page_id)
    return response["results"]


def get_todo(client: Client, all_blocks: List[Dict[str, str]]):
    """Get all todo blocks given a list of blocks, if block has children, use recursion"""
    to_do_blocks = []
    for block in all_blocks:
        result = {}
        if block["type"] == "to_do":
            to_do_block = block["to_do"]
            checked = to_do_block["checked"]
            content = to_do_block["rich_text"][0]["plain_text"]

            if not checked:
                result["status"] = "needsAction"
            else:
                result["status"] = "completed"

            result["title"] = content
            result["id"] = block["id"]

            to_do_blocks.append(result)

        if block["has_children"]:
            nested_block = get_all_blocks(client, block["id"])
            nested_result = get_todo(client, nested_block)
            to_do_blocks.extend(nested_result)

    if not to_do_blocks:
        logging.info("No Todo blocks have been found")

    return to_do_blocks


# -----------------
# AUTH FUNCTION


def authenticate_and_print():
    """Authentication with Google"""
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        creds = flow.run_local_server(port=0)

        with open("token.json", "w") as token:
            token.write(creds.to_json())

    try:
        service = build("tasks", "v1", credentials=creds)
    except HttpError as err:
        logging.error(err)

    return service


# -----------------
# NOTION-GOOGLE API CALLS


def insert_notion_tasks_in_google_tasks(service, notion_tasks, task_list_id):
    """Insert notion tasks in Google, if they are not already there"""

    for notion_task in notion_tasks[::-1]:
        if r.get(notion_task["id"]) is None:
            service.tasks().insert(tasklist=task_list_id, body=notion_task).execute()


def update_google_tasks(service, notion_tasks, task_list_id):
    """Function that Updates tasks. Closes tasks marked as completed from Notion to Google Takss"""

    for notion_task in notion_tasks:

        if r.get(notion_task["id"]) is not None:
            service.tasks().patch(
                tasklist=task_list_id,
                task=r.get(notion_task["id"]),
                body={"status": notion_task["status"], "title": notion_task["title"]},
            ).execute()


def create_notion_tasklist(service, title) -> str:
    """Create a dedicated TaskList in Google Tasks if it does not exist"""

    for task_list in service.tasklists().list().execute()["items"]:
        if task_list["title"] == title:
            return task_list["id"]

    new_task_list = service.tasklists().insert(body={"title": title}).execute()
    return new_task_list["id"]


# ------
# REDIS HANDLING


def add_id_mapping_to_redis(service, notion_tasks, task_list_id):
    """Add key-value mapping between Notion todo ids and Google todo ids"""

    current_google_tasks = [
        {"title": task["title"], "id": task["id"], "status": task["status"]}
        for task in service.tasks().list(tasklist=task_list_id).execute()["items"]
    ]

    for notion_task in notion_tasks:
        for google_task in current_google_tasks:

            if r.get(notion_task["id"]):
                logging.info("ID already in Redis, skipping")
                continue

            elif (
                notion_task["title"] == google_task["title"]
                and notion_task["status"] == google_task["status"]
            ):
                r.set(notion_task["id"], google_task["id"])
                r_reverse.set(
                    google_task["id"], notion_task["id"]
                )  # store reverse mapping in db1

                logging.info(
                    f"Successfully added k: {notion_task['id']} v: {google_task['id']}"
                )


def remove_deleted_tasks_ids_from_redis(service, notion_tasks, task_list_id):
    """Remove a deleted Notion task from Google and Redis"""
    current_notion_ids = []
    for notion_task in notion_tasks:
        current_notion_ids.append(notion_task["id"])

    for notion_id_in_db in r.keys():
        if notion_id_in_db not in current_notion_ids:

            service.tasks().delete(
                tasklist=task_list_id, task=r.get(notion_id_in_db)
            ).execute()
            google_task_id = r.get(notion_id_in_db)
            r.delete(notion_id_in_db)
            r_reverse.delete(google_task_id)
