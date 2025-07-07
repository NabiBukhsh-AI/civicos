# Municipal AI Assistant

A FastAPI-based AI assistant designed for municipal administration, enabling users to query PDF documents via a chatbot and analyze images for civic issues with metadata extraction. Powered by Google’s Gemini AI, LangChain, and FAISS, it provides actionable insights for infrastructure management and decision-making.

## Table of Contents
- [Features](#features)
- [Project Structure](#project-structure)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [API Endpoints](#api-endpoints)
- [Contributing](#contributing)
- [License](#license)
- [Contact](#contact)

## Features
- **PDF-Based Chatbot**: Query PDF documents using natural language, leveraging Google’s Gemini AI and FAISS for vector-based search.
- **Image Analysis**: Analyze images for civic issues (e.g., infrastructure damage) with concise reports on scenes, maintenance needs, and safety concerns.
- **Metadata Extraction**: Extract EXIF metadata (e.g., GPS coordinates, datetime) from images for detailed reporting.
- **FastAPI Backend**: Robust API endpoints for chatbot interactions and image processing, with CORS support for flexible client integration.
- **Scalable Design**: Modular structure with separate API logic, models, and routes for easy maintenance and extension.

## Project Structure
```
Municipal-AI-Assistant/
├── api/
│   ├── chatbot.py           # Core chatbot logic (PDF processing, FAISS)
│   ├── image_processor.py   # Core image analysis logic (Gemini AI)
│   ├── metadata_extractor.py # EXIF metadata extraction
├── models/
│   ├── chatbot.py           # Pydantic models for chatbot API
│   ├── image_processor.py   # Pydantic model for image analysis API
├── routes/
│   ├── chatbot.py           # FastAPI router for chatbot endpoint
│   ├── image_processor.py   # FastAPI router for image processing endpoint
├── create_embeddings.py     # Script to generate FAISS embeddings from PDFs
├── main.py                  # Main FastAPI application
├── requirements.txt         # Python dependencies
├── .gitignore              # Git ignore file
├── .env                    # Environment variables (not tracked)
└── README.md               # Project documentation
```

## Installation
1. **Clone the Repository**:
   ```bash
   git clone https://github.com/NabiBukhsh-AI/Municipal-AI-Assistant.git
   cd Municipal-AI-Assistant
   ```

2. **Set Up a Virtual Environment** (recommended):
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install Dependencies**:
   Ensure Python 3.8+ is installed. Install required packages:
   ```bash
   pip install -r requirements.txt
   ```

4. **Set Up Environment Variables**:
   Create a `.env` file in the project root with your Google API key:
   ```bash
   GOOGLE_API_KEY=your_google_api_key
   ```
   Obtain your API key from [Google Cloud Console](https://console.cloud.google.com).

## Configuration
1. **Generate FAISS Embeddings**:
   Update `create_embeddings.py` with the paths to your PDF files (replace `[Folder]/[FILE(s)]`):
   ```python
   pdf_files = ["path/to/your/document1.pdf", "path/to/your/document2.pdf"]
   ```
   Run the script to create embeddings:
   ```bash
   python create_embeddings.py
   ```
   This generates a `faiss_index` directory for the chatbot’s vector store.

2. **Run the FastAPI Application**:
   Start the server:
   ```bash
   uvicorn main:app --host 127.0.0.1 --port 8000
   ```
   Access the API at `http://127.0.0.1:8000` or use the interactive docs at `http://127.0.0.1:8000/docs`.

## Usage
1. **Chatbot**:
   - Send a POST request to `/chatbot/` with a JSON body:
     ```json
     {
       "question": "What is the main topic of the document?",
       "session_id": "optional-session-id"
     }
     ```
   - Example using `curl`:
     ```bash
     curl -X POST "http://127.0.0.1:8000/chatbot/" -H "Content-Type: application/json" -d '{"question": "What is the main topic of the document?"}'
     ```

2. **Image Processing**:
   - Send a POST request to `/image/process` with image files and an optional context:
     ```bash
     curl -X POST "http://127.0.0.1:8000/image/process" -F "images=@path/to/image1.jpg" -F "images=@path/to/image2.jpg" -F "context=Road inspection"
     ```
   - Returns a combined analysis with metadata (e.g., GPS coordinates, datetime).

3. **Health Check**:
   - Check the API status:
     ```bash
     curl http://127.0.0.1:8000/health
     ```

## API Endpoints
- **POST /chatbot/**: Query the chatbot with a question based on pre-embedded PDFs.
- **POST /image/process**: Analyze uploaded images and extract metadata (e.g., GPS, datetime).
- **GET /health**: Check API health status.

## Contributing
Contributions are welcome! To contribute:
1. Fork the repository.
2. Create a new branch (`git checkout -b feature/your-feature`).
3. Commit your changes (`git commit -m 'Add your feature'`).
4. Push to the branch (`git push origin feature/your-feature`).
5. Open a Pull Request.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) for more details (create this file if you wish to formalize contribution guidelines).

## License
This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Contact
Created by [NabiBukhsh-AI](https://github.com/NabiBukhsh-AI). For feedback or suggestions, open an issue or contact me via GitHub.