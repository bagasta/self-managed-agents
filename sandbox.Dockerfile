FROM nikolaik/python-nodejs:python3.12-nodejs22

RUN apt-get update && apt-get install -y --no-install-recommends \
    zip unzip curl wget git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    openpyxl \
    xlsxwriter \
    reportlab \
    fpdf2 \
    python-docx \
    pandas \
    pillow \
    requests \
    beautifulsoup4 \
    lxml \
    matplotlib \
    jinja2
