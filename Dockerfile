FROM ubuntu:24.04

# Prevent interactive prompts during apt install
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Update and install python, pip, and required system tools
RUN apt-get update && \
    apt-get install -y python3 python3-pip python3-venv tzdata && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Create a virtual environment (required in newer Ubuntu versions to avoid PEP 668 conflicts)
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy package requirements and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Expose the web and streamable-HTTP MCP ports
EXPOSE 8000 8787

# Start the web server by default
CMD ["python", "web_server.py"]
