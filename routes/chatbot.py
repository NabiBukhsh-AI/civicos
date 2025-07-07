from fastapi import APIRouter, HTTPException
from models.chatbot import ChatRequest, ChatResponse
from api.chatbot import Chatbot

router = APIRouter()
chatbot = Chatbot()

@router.post("/", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    """
    Endpoint for chatbot interaction using pre-embedded FAISS vector store.
    """
    answer = chatbot.answer_question(request.question)
    return ChatResponse(answer=answer, session_id=request.session_id)