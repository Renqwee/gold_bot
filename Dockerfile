FROM python:3.14-slim

WORKDIR /gold_bot

COPY ./requirements.txt .

RUN pip install -r requirements.txt

COPY . /gold_bot

CMD ["python", "bot.py"]