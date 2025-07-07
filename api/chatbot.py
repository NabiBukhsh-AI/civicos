from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain.prompts import PromptTemplate
from langchain_community.vectorstores import FAISS
from langchain.chains.question_answering import load_qa_chain
from langchain.text_splitter import RecursiveCharacterTextSplitter
from fastapi import HTTPException
from PyPDF2 import PdfReader
from dotenv import load_dotenv
import os

load_dotenv()

if not os.getenv("GOOGLE_API_KEY"):
    raise ValueError("GOOGLE_API_KEY not found in environment variables. Please check your .env file.")

class Chatbot:
    def __init__(self, vector_store_path: str = "faiss_index"):
        self.embeddings = GoogleGenerativeAIEmbeddings(model="models/embedding-001", google_api_key=os.getenv("GOOGLE_API_KEY"))
        self.vector_db = FAISS.load_local(vector_store_path, self.embeddings, allow_dangerous_deserialization=True)
        self.model = ChatGoogleGenerativeAI(
            model="gemini-1.5-flash",
            google_api_key=os.getenv("GOOGLE_API_KEY"),
            temperature=0.3,
        )
        self.prompt_template = PromptTemplate(
            template="""
            Answer the question as detailed as possible from the provided context, make sure to provide all the details.

            Context:
            {context}

            Question:
            {question}

            Answer:
            """,
            input_variables=["context", "question"]
        )

    def get_conversational_chain(self):
        return load_qa_chain(llm=self.model, chain_type="stuff", prompt=self.prompt_template)

    def answer_question(self, question: str):
        try:
            docs = self.vector_db.similarity_search(question)
            chain = self.get_conversational_chain()
            result = chain({"input_documents": docs, "question": question}, return_only_outputs=True)
            return result.get("output_text", "No response generated.")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Chatbot query failed: {str(e)}")

def create_and_save_embeddings(pdf_paths: list[str], persist_dir: str = "faiss_index"):
    """
    Process PDFs, create embeddings, and save FAISS vector store locally.

    Args:
        pdf_paths (list[str]): List of file paths to PDF documents.
        persist_dir (str): Directory to save the FAISS vector store (default: 'faiss_index').

    Returns:
        None
    """
    try:
        os.makedirs(persist_dir, exist_ok=True)

        text = ""
        for pdf_path in pdf_paths:
            if not os.path.exists(pdf_path):
                raise FileNotFoundError(f"PDF file not found: {pdf_path}")
            reader = PdfReader(pdf_path)
            for page in reader.pages:
                content = page.extract_text()
                if content:
                    text += content

        splitter = RecursiveCharacterTextSplitter(chunk_size=10000, chunk_overlap=1000)
        chunks = splitter.split_text(text)

        embeddings = GoogleGenerativeAIEmbeddings(model="models/embedding-001", google_api_key=os.getenv("GOOGLE_API_KEY"))
        vector_store = FAISS.from_texts(chunks, embedding=embeddings)
        vector_store.save_local(persist_dir)
        print(f"Embeddings created and saved to {persist_dir}")
    except PermissionError as e:
        print(f"Permission error: Unable to write to {persist_dir}. Ensure you have write permissions. Error: {str(e)}")
    except Exception as e:
        print(f"Error creating embeddings: {str(e)}")