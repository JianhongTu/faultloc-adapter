# Container the agent runs in. Mirrors FLBench's eval agent image
# (src/flbench/eval/image/Dockerfile) so the tools the prompt advertises are
# actually present and agent behaviour is comparable to the reference.
#
# The agent CLIs are baked in rather than installed by Harbor at run time: the
# task sets [environment] network_mode = "no-network", and that baseline applies
# during agent.setup(). Harbor's installed agents skip installation when a
# satisfying binary is already present, so baking them keeps setup offline.
# Pinned: the agent harness must not drift between runs, or measured differences
# between conditions are confounded by a changed agent.
FROM ubuntu@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    curl wget git python3 python3-pip \
    vim-common bsdmainutils \
    file binutils \
    less tree \
    ripgrep jq universal-ctags cscope \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@2.1.220 @openai/codex@0.145.0

ENV NODE_PATH=/usr/lib/node_modules

# Calls the PoC sidecar; the agent never executes the reproducer itself.
COPY run_poc.sh /usr/local/bin/run_poc.sh
RUN chmod +x /usr/local/bin/run_poc.sh
