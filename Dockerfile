FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SECRET_KEY (required) must be supplied at runtime — see .env.example. Not
# baked into the image.
ENV FLASK_ENV=production
EXPOSE 5053

CMD ["waitress-serve", "--host=0.0.0.0", "--port=5053", "wsgi:app"]
