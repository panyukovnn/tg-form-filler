FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
COPY rules/ rules/

RUN pip install --no-cache-dir .

CMD ["python", "-m", "tg_form_filler"]
