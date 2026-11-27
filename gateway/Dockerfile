FROM kong:3.9

USER root

COPY kong.yml.template /etc/kong/kong.yml.template
COPY entrypoint.sh /usr/local/bin/gateway-entrypoint.sh

RUN sed -i 's/\r$//' /usr/local/bin/gateway-entrypoint.sh /etc/kong/kong.yml.template \
    && chmod 755 /usr/local/bin/gateway-entrypoint.sh \
    && chown kong:kong /etc/kong/kong.yml.template /usr/local/bin/gateway-entrypoint.sh

USER kong

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD kong health || exit 1

ENTRYPOINT ["/usr/local/bin/gateway-entrypoint.sh"]
