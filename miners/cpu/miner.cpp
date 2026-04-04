#include <iostream>
#include <cstring>
#include <openssl/sha.h>
#include <stdlib.h>

// Compile with: g++ -O3 -march=native -shared -o libminer.so -fPIC miner.cpp -lcrypto

// Ultra-fast integer to string conversion (bypasses slow snprintf)
inline int fast_itoa(unsigned long long value, char* buffer) {
    if (value == 0) {
        buffer[0] = '0';
        return 1;
    }
    char temp[32];
    char *p = temp;
    while (value > 0) {
        *p++ = (char)(value % 10) + '0';
        value /= 10;
    }
    int len = p - temp;
    for (int i = 0; i < len; ++i) {
        buffer[i] = *--p;
    }
    return len;
}

extern "C" {
    long long mine_c(
        const unsigned char* prefix, int prefix_len,
        const unsigned char* suffix, int suffix_len,
        const unsigned char* target_bytes,
        unsigned long long start_nonce,
        unsigned long long attempts,
        unsigned char* out_hash
    ) {
        unsigned char hash[SHA256_DIGEST_LENGTH];
        char nonce_str[32];

        // OPTIMIZATION 1: Pre-compute the SHA-256 state for the prefix!
        // This avoids hashing the same transaction list millions of times per second.
        SHA256_CTX base_ctx;
        SHA256_Init(&base_ctx);
        SHA256_Update(&base_ctx, prefix, prefix_len);

        for (unsigned long long nonce = start_nonce; nonce < start_nonce + attempts; ++nonce) {

            // OPTIMIZATION 2: Fast integer conversion
            int nonce_len = fast_itoa(nonce, nonce_str);

            // Clone the pre-computed hash state
            SHA256_CTX ctx = base_ctx;

            // Only hash the nonce and the suffix!
            SHA256_Update(&ctx, nonce_str, nonce_len);
            SHA256_Update(&ctx, suffix, suffix_len);
            SHA256_Final(hash, &ctx);

            // OPTIMIZATION 3: Fast-fail target check (most hashes fail on byte 0)
            if (hash[0] > target_bytes[0]) continue;

            // Deep target check for potential winners
            bool meets_target = true;
            for (int i = 0; i < 32; i++) {
                if (hash[i] < target_bytes[i]) {
                    break; // Smaller than target, valid!
                } else if (hash[i] > target_bytes[i]) {
                    meets_target = false; // Larger than target, invalid.
                    break;
                }
            }

            if (meets_target) {
                memcpy(out_hash, hash, SHA256_DIGEST_LENGTH);
                return nonce;
            }
        }

        return -1; // No hit in this batch
    }
}
