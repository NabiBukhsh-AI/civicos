from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import PromptTemplate
from fastapi import HTTPException
import base64
import os

class ImageProcessor:
    def __init__(self):
        self.model = ChatGoogleGenerativeAI(
            model="gemini-1.5-flash",
            google_api_key=os.getenv("GOOGLE_API_KEY"),
            temperature=0.7,
        )
        self.prompt_template = PromptTemplate(
            input_variables=["context"],
            template="""
            As a municipal administrator's assistant, analyze the provided images and provide a single, concise analysis covering all images:
            1. Description of the scenes
            2. Infrastructure or civic issues visible
            3. Condition assessment and maintenance needs
            4. Required actions or improvements
            5. Safety concerns if any
            
            The response should be concise and clear, summarizing all images in 3-5 lines.
            
            Context: {context}
            
            Provide a structured, clear analysis.
            """
        )

    async def analyze_images(self, image_bytes_list: list[bytes], context: str = ""):
        try:
            # Encode all images as base64
            content = [{"type": "text", "text": self.prompt_template.format(context=context)}]
            for image_bytes in image_bytes_list:
                base64_image = base64.b64encode(image_bytes).decode('utf-8')
                content.append({"type": "image_url", "image_url": f"data:image/jpeg;base64,{base64_image}"})
            
            message = HumanMessage(content=content)
            response = self.model.invoke([message])
            return response.content
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Image analysis failed: {str(e)}")