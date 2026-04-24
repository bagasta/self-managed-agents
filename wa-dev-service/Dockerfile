FROM golang:1.25-alpine AS builder
WORKDIR /app
RUN apk add --no-cache gcc musl-dev sqlite-dev
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=1 go build -o wa-dev-service .

FROM alpine:latest
WORKDIR /app
RUN apk add --no-cache ca-certificates sqlite-libs
COPY --from=builder /app/wa-dev-service .
COPY dashboard/ ./dashboard/
RUN mkdir -p /data/wa-dev-store
EXPOSE 8081
CMD ["./wa-dev-service"]
