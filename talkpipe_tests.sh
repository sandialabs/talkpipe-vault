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

test_tika() {
    echo "=== Testing Tika ==="
    
    echo "Checking Java installation..."
    if java -version > /dev/null 2>&1; then
        java -version 2>&1 | head -1 | sed 's/^/  /'
        echo "✓ Java is installed"
    else
        echo "✗ Java is not installed"
        return 1
    fi
    
    echo "Checking Tika Python package..."
    if python3 -c "import tika" 2>/dev/null; then
        echo "✓ Tika Python package is installed"
    else
        echo "✗ Tika Python package is not installed"
        return 1
    fi
    
    echo "Testing Tika server (this may take a moment on first run)..."
    python3 << 'PYTHON_EOF'
import sys
import tempfile
import os
from tika import parser

try:
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write('Hello, this is a test file for Tika text extraction.')
        test_file = f.name
    
    try:
        parsed = parser.from_file(test_file)
        if parsed and 'content' in parsed:
            content = parsed['content'].strip()
            if content:
                print("✓ Tika server is working!")
                print(f"  Extracted content: {content[:60]}...")
                if 'metadata' in parsed:
                    print(f"  Content type: {parsed['metadata'].get('Content-Type', 'unknown')}")
            else:
                print("⚠ Tika server responded but extracted empty content")
        else:
            print("⚠ Tika server responded but no content in result")
    except Exception as e:
        print(f"✗ Tika test failed: {e}")
        sys.exit(1)
    finally:
        os.unlink(test_file)
except Exception as e:
    print(f"✗ Tika test setup failed: {e}")
    sys.exit(1)
PYTHON_EOF
    
    echo ""
}

alias test-all='test_network && test_ollama && test_tika'
alias test-net='test_network'
alias test-ollama='test_ollama'
alias test-tika='test_tika'

echo "Test functions loaded! Available commands:"
echo "  test_network  - Test network connectivity"
echo "  test_ollama   - Test Ollama connectivity"
echo "  test_tika     - Test Tika functionality"
echo "  test-all      - Run all tests"
echo ""
echo "Or use the aliases: test-net, test-ollama, test-tika"
echo ""

