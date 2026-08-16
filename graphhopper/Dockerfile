# There is no official graphhopper/graphhopper Docker image (confirmed:
# no Dockerfile anywhere in the graphhopper/graphhopper repo tree, and
# GraphHopper's own docs only cover building from Maven source or
# running the release jar with `java -jar`). This containerizes the
# official pre-built release jar from GitHub Releases -- no Maven
# build, no compiling GraphHopper's own source.
FROM eclipse-temurin:21-jre

WORKDIR /app

ARG GRAPHHOPPER_VERSION=11.0
ADD https://github.com/graphhopper/graphhopper/releases/download/${GRAPHHOPPER_VERSION}/graphhopper-web-${GRAPHHOPPER_VERSION}.jar /app/graphhopper-web.jar

EXPOSE 8989

ENTRYPOINT ["java", "-jar", "/app/graphhopper-web.jar", "server", "/app/config.yml"]
