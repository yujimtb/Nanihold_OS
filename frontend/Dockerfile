FROM node:22-slim

WORKDIR /workspace

COPY package.json package-lock.json* ./
RUN npm install

COPY . .

EXPOSE 5173

