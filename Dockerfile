# Gunakan Python 3.11 sebagai base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependensi sistem yang diperlukan
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements.txt terlebih dahulu untuk memanfaatkan cache layer Docker
COPY requirements.txt .

# Install dependensi Python
RUN pip install --no-cache-dir -r requirements.txt

# Copy seluruh kode aplikasi
COPY . .

# Buat direktori untuk logs
RUN mkdir -p logs

# Set environment variables
ENV FLASK_APP=main.py
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

# Expose port yang digunakan aplikasi (default Flask port)
EXPOSE 5555

# Command untuk menjalankan aplikasi
CMD ["python", "main.py"]