FROM node:24-alpine
WORKDIR /app
COPY package.json package-lock.json /app/
RUN npm ci
COPY src /app/src
USER node
CMD ["node", "--import", "tsx", "src/index.ts"]
