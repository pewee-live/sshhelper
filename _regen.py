# -*- coding: utf-8 -*-
"""Regenerate all cases with upgraded template."""
import os, asyncio, sys
os.chdir(r"D:\develop\vscode\ws1\sshhelper")
from dotenv import load_dotenv
load_dotenv()

from langchain_core.messages import messages_from_dict
from case_generator import generate_from_all_sessions
from web_server import SESSION_MANAGER, clean_message_history

async def main():
    count = await generate_from_all_sessions(SESSION_MANAGER)
    print("Generated %d cases total." % count)

asyncio.run(main())