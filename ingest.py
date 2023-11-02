from fastapi import FastAPI, APIRouter, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette_validation_uploadfile import ValidateUploadFileMiddleware
from langchain.document_loaders import UnstructuredFileLoader
from unstructured.cleaners.core import clean_extra_whitespace
from langchain.text_splitter import RecursiveCharacterTextSplitter
from transformers import GPT2TokenizerFast

from pydantic import BaseModel
from typing import List, Mapping, Any, Optional
import tempfile
import os
import hashlib
import nltk
import gzip
import magic

delete_temp_file = bool(os.getenv("DELETE_TEMP_FILE", ""))
nltk_data = os.getenv("NLTK_DATA")
model = os.getenv("MODEL")
max_file_size_in_mb = int(os.getenv("MAX_FILE_SIZE_IN_MB"))
supported_file_types = os.getenv("SUPPORTED_FILE_TYPES")
chunk_size = int(os.getenv("CHUNK_SIZE"))
chunk_overlap = int(os.getenv("CHUNK_OVERLAP"))

print("delete_temp_file:", delete_temp_file)
print("nltk_data:", nltk_data)
print("model:", model)
print("max_file_size_in_mb:", max_file_size_in_mb)
print("supported_file_types:", supported_file_types)
print("chunk_size:", chunk_size)
print("chunk_overlap:", chunk_overlap)

# Set the nltk.data.path with environment variable
nltk.data.path.append(nltk_data)

router = APIRouter()
tokenizer = GPT2TokenizerFast.from_pretrained(model)


def create_app():
    app = FastAPI()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(
        ValidateUploadFileMiddleware,
        app_path="/ingest",
        max_size=max_file_size_in_mb * 1048576,  # 1 MB
        file_type=supported_file_types.split(",")
    )

    app.add_middleware(GZipMiddleware, minimum_size=1000)

    app.include_router(router)

    return app


def is_gz_file(file_path):
    with open(file_path, 'rb') as f:
        return f.read(2) == b'\x1f\x8b'


def get_mime_type(file_path):
    mime_type = magic.from_file(file_path, mime=True)
    return mime_type


def count_tokens(text: str):
    encoded = tokenizer.encode(text)
    return len(encoded)


def load(file):
    # REF: https://python.langchain.com/docs/integrations/document_loaders/unstructured_file
    if is_gz_file(file.name):
        print(f'The {file.name} is gzip compressed.')
        with gzip.open(file.name, 'rb') as gzipped_file:
            with tempfile.NamedTemporaryFile(
                    mode='wb',
                    delete=delete_temp_file) as decompressed_file:
                for chunk in gzipped_file:
                    decompressed_file.write(chunk)
                decompressed_file.flush()
                loader = UnstructuredFileLoader(
                    file_path=decompressed_file.name,
                    post_processors=[clean_extra_whitespace],
                )
                return loader.load(), get_mime_type(decompressed_file.name)
    else:
        loader = UnstructuredFileLoader(
            file_path=file.name,
            post_processors=[clean_extra_whitespace],
        )
        return loader.load(), get_mime_type(file.name)


md5 = hashlib.md5()


def get_doc_id(doc):
    md5.update(doc.metadata['source'].encode('utf-8'))
    uid = md5.hexdigest()[:12]
    return uid


def split(doc):
    text_splitter = RecursiveCharacterTextSplitter.from_huggingface_tokenizer(
        tokenizer=tokenizer,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    chunks = text_splitter.split_text(doc.page_content)
    return chunks


class DocumentItem(BaseModel):
    content: str
    tokens_count: int
    metadata: Mapping[str, Any]


class Document(BaseModel):
    content: Optional[str]
    tokens_count: int
    mime_type: str
    items: List[DocumentItem]


@router.post("/ingest")
async def load_split_count(file: UploadFile = File(...)):
    file_chunk_size = 1024 * 1024  # 1 MB

    with tempfile.NamedTemporaryFile(
            mode='wb',
            buffering=file_chunk_size,
            delete=delete_temp_file) as temp:
        while True:
            chunk = await file.read(file_chunk_size)
            if not chunk:
                break
            temp.write(chunk)
        temp.flush()
        docs, mime_type = load(temp)
        print("mime_type", mime_type)
        doc = docs[0]
        doc_tokens_count = count_tokens(doc.page_content)
        items = []
        if (doc_tokens_count > chunk_size):
            texts = split(doc)
            print(len(texts))
            print(texts[1])
            id = get_doc_id(doc)
            for i, text in enumerate(texts):
                items.append(
                    DocumentItem(
                        content=text,
                        tokens_count=count_tokens(text),
                        metadata={'id': f'{id}-{i}'},
                    )
                )
        else:
            print('doc_tokens_count <= chunk_size: doc.page_content:',
                  doc.page_content)
        document = Document(
            # None when the source doc is text/plain
            content=doc.page_content if mime_type != "text/plain" else None,
            tokens_count=doc_tokens_count,
            mime_type=mime_type,
            items=items,
        )
        return document


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        create_app(), host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", 8000))
    )
elif os.getenv("RUNTIME") == "aws-lambda":
    from mangum import Mangum
    handler = Mangum(create_app())
