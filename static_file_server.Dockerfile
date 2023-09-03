FROM nginx:alpine3.18-slim

WORKDIR /usr/src/app

COPY . .

COPY ./nginx.conf /etc/nginx/nginx.conf