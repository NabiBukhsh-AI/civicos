from dotenv import load_dotenv
from api.chatbot import create_and_save_embeddings
import os

load_dotenv()

if not os.getenv("GOOGLE_API_KEY"):
    raise ValueError("GOOGLE_API_KEY not found in environment variables. Please check your .env file.")

pdf_files = ["[Folder]/[FILE(s)]"]

create_and_save_embeddings(pdf_files, persist_dir="faiss_index")