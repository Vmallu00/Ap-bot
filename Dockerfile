FROM python:3.10-slim

WORKDIR /app

# Install SSH client (for paramiko to work) – optional, but may help
RUN apt-get update && apt-get install -y openssh-client && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# The SSH private key will be mounted as a volume at /app/ssh_key
VOLUME /app/ssh_key

CMD ["python", "bot.py"]
