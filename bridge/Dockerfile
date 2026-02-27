FROM python:3.11-slim

WORKDIR /app

# Install dependencies (layer caching: requirements.txt first)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bridge source code
COPY . .

# Expose bridge server port
EXPOSE 7777

# Run bridge server (without TTS/STT flags, which need macOS)
CMD ["python", "-m", "bridge", "run"]
