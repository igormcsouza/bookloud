# Production-equivalent Next.js server (server-rendered) for docker-compose's
# `ui` profile — mirrors the OpenNext/Lambda SSR target, but plain `next
# start`. Not the Lambda deploy path (that's OpenNext, built by CI). For
# lightweight day-to-day work, `npm run dev` is the alternative.
FROM node:24-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
RUN npm run build

FROM node:24-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/package.json /app/package-lock.json ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next
COPY --from=build /app/public ./public
EXPOSE 3000
CMD ["npm", "run", "start"]
