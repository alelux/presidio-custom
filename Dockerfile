FROM mcr.microsoft.com/presidio-analyzer:latest

RUN python -m spacy download en_core_web_md && \
    python -m spacy download fr_core_news_md && \
    python -m spacy download it_core_news_md

COPY nlp_config.yaml /app/presidio_analyzer/conf/default.yaml
