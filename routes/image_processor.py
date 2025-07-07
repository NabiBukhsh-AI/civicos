from fastapi import APIRouter, File, UploadFile, HTTPException
from typing import List
from models.image_processor import ImageResult
from api.image_processor import ImageProcessor
from api.metadata_extractor import MetadataExtractor

router = APIRouter()
image_processor = ImageProcessor()

@router.post("/process", response_model=ImageResult)
async def process_images(
    images: List[UploadFile] = File(...),
    context: str = ""
):
    """
    Endpoint for image analysis and metadata extraction, providing a single analysis with indexed metadata.
    """
    try:
        image_data = []
        for image in images:
            contents = await image.read()
            image_data.append((contents, image.filename))
        
        image_bytes_list = [data[0] for data in image_data]
        analysis = await image_processor.analyze_images(image_bytes_list, context)
        
        metadata_list = MetadataExtractor.extract_metadata_list(image_data)
        
        metadata_str = "Photo Details:\n"
        for meta in metadata_list:
            metadata_str += f"{meta['index']}. "
            metadata_str += f"Filename: {meta['filename']}"
            if "latitude" in meta and "longitude" in meta:
                metadata_str += f", Latitude: {meta['latitude']}, Longitude: {meta['longitude']}"
            if "datetime" in meta:
                metadata_str += f", Datetime: {meta['datetime']}"
            if "error" in meta:
                metadata_str += f", Error: {meta['error']}"
            metadata_str += "\n"
        
        combined_analysis = f"{analysis}\n\n{metadata_str.strip()}"
        
        return ImageResult(analysis=combined_analysis)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))