from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routes.chatbot import router as chatbot_router
from routes.image_processor import router as image_router
from dotenv import load_dotenv
import os

load_dotenv()

if not os.getenv("GOOGLE_API_KEY"):
    raise ValueError("GOOGLE_API_KEY not found in environment variables. Please check your .env file.")

app = FastAPI(title="Chatbot and Image Analysis API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chatbot_router, prefix="/chatbot", tags=["Chatbot"])
app.include_router(image_router, prefix="/image", tags=["Image Processor"])

@app.get("/health")
async def health_check():
    """
    Health check endpoint.
    """
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)