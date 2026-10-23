ARG JAVA_IMAGE=eclipse-temurin:17-jre-jammy
FROM ${JAVA_IMAGE}
RUN groupadd --gid 1000 minecraft && useradd --uid 1000 --gid 1000 --no-create-home minecraft \
    && mkdir -p /data /opt/minecraft && chown 1000:1000 /data
RUN command -v timeout && command -v flock && command -v stat
COPY .cache/server-1.20.4.jar /opt/minecraft/server.jar
COPY server.properties /opt/minecraft/server.properties
COPY spigot.yml /opt/minecraft/spigot.yml
COPY server-entrypoint.sh /opt/minecraft/entrypoint.sh
COPY harness-control /usr/local/bin/harness-control
RUN chmod 0755 /usr/local/bin/harness-control
USER 1000:1000
WORKDIR /data
EXPOSE 25565
ENTRYPOINT ["/bin/sh", "/opt/minecraft/entrypoint.sh"]
