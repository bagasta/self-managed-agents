FROM golang:1.25-alpine AS builder
WORKDIR /app
RUN apk add --no-cache gcc musl-dev sqlite-dev
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=1 go build -o wa-service .

FROM alpine:latest
WORKDIR /app
RUN apk add --no-cache ca-certificates sqlite-libs
COPY --from=builder /app/wa-service .
RUN mkdir -p /data/wa-store
EXPOSE 8080
CMD ["./wa-service"]
