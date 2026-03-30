# Dockerfile

# Use x86 platform for monero binary compatibility
FROM --platform=linux/amd64 python:3.11-slim

WORKDIR /app

COPY requirements.txt .

# Install system dependencies + supervisor + wget for monero binaries
RUN apt-get update && apt-get install -y \
    build-essential \
    pkg-config \
    git \
    supervisor \
    wget \
    bzip2 \
    && rm -rf /var/lib/apt/lists/*

# Download monero-wallet-rpc
ARG MONERO_VERSION=v0.18.3.4
RUN wget -q https://downloads.getmonero.org/cli/monero-linux-x64-${MONERO_VERSION}.tar.bz2 \
    && tar -xjf monero-linux-x64-${MONERO_VERSION}.tar.bz2 \
    && mv monero-x86_64-linux-gnu-${MONERO_VERSION}/monero-wallet-rpc /usr/local/bin/ \
    && rm -rf monero-linux-x64-${MONERO_VERSION}.tar.bz2 monero-x86_64-linux-gnu-${MONERO_VERSION}

# Create wallet directory
RUN mkdir -p /wallet

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Copy supervisord config
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

EXPOSE 8000

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
