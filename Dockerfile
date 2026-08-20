FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for better Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Render/Railway/Fly all inject the port via the PORT env var.
# Default to 8000 for local `docker run` testing.
ENV PORT=8000
EXPOSE 8000

# Use a shell form so ${PORT} expands correctly at container start.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}