# Test functions for TalkPipe Vault container

test_network() {
    echo "=== Testing Network Connectivity ==="
    echo "Testing external connectivity..."
    if curl -s --max-time 3 https://www.google.com > /dev/null 2>&1; then
        echo "✓ External network connectivity: OK"
    else
        echo "✗ External network connectivity: FAILED"
    fi
    
    echo "Testing DNS resolution..."
    if nslookup google.com > /dev/null 2>&1; then
        echo "✓ DNS resolution: OK"
    else
        echo "✗ DNS resolution: FAILED"
    fi
    
    echo "Testing localhost connectivity..."
    if ping -c 1 -W 1 127.0.0.1 > /dev/null 2>&1; then
        echo "✓ Localhost connectivity: OK"
    else
        echo "✗ Localhost connectivity: FAILED"
    fi
    echo ""
}

test_ollama() {
    echo "=== Testing Ollama Connectivity ==="
    echo "Testing Ollama API endpoint..."
    
    OLLAMA_URLS=(
        "http://localhost:11434/api/tags"
        "http://127.0.0.1:11434/api/tags"
        "http://host.containers.internal:11434/api/tags"
    )
    
    FOUND=false
    for url in "${OLLAMA_URLS[@]}"; do
        if response=$(curl -s --max-time 2 "$url" 2>/dev/null); then
            if echo "$response" | grep -q "models"; then
                echo "✓ Ollama is accessible at: $url"
                echo "  Available models:"
                echo "$response" | python3 -c "import sys, json; data=json.load(sys.stdin); [print('    -', m['name']) for m in data.get('models', [])]" 2>/dev/null || echo "$response" | head -5
                FOUND=true
                break
            fi
        fi
    done
    
    if [ "$FOUND" = false ]; then
        echo "✗ Ollama is not accessible"
        echo "  Tried: ${OLLAMA_URLS[*]}"
        echo "  Make sure Ollama is running on the host"
    fi
    echo ""
}

alias test-all='test_network && test_ollama'
alias test-net='test_network'
alias test-ollama='test_ollama'

echo "Test functions loaded! Available commands:"
echo "  test_network  - Test network connectivity"
echo "  test_ollama   - Test Ollama connectivity"
echo "  test-all      - Run all tests"
echo ""
echo "Or use the aliases: test-net, test-ollama"
echo ""

