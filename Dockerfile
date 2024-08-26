## For dev:
## docker build -t osmsg . 
## docker run --rm -v $(pwd)/osmsg:/app/osmsg --name osmsg-container osmsg

ARG PYTHON_VERSION=3.11

# Build stage
FROM docker.io/python:${PYTHON_VERSION}-slim-bookworm AS build-stage

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    build-essential cmake libboost-dev \
    libexpat1-dev zlib1g-dev libbz2-dev \
    && apt-get autoremove -y && apt-get clean

# Copy project files
WORKDIR /app

COPY osmsg /app/osmsg
COPY pyproject.toml /app/pyproject.toml
COPY README.md /app/README.md
COPY data /app/data

# Install the package
RUN pip install -e .

# Final stage
FROM docker.io/python:${PYTHON_VERSION}-slim-bookworm

COPY --from=build-stage /usr/local /usr/local
# COPY --from=build-stage /usr/local/osmium /usr/l/osmium

# ENV PATH="/usr/bin/osmium:${PATH}"

WORKDIR /app

COPY --from=build-stage /app /app

CMD ["/bin/bash"]
