FROM node:22-slim

WORKDIR /workspace

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .

EXPOSE 5173

