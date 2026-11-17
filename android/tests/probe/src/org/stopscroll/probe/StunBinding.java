package org.stopscroll.probe;

import java.nio.ByteBuffer;
import java.security.SecureRandom;

final class StunBinding {
    private static final int HEADER_BYTES = 20;
    private static final int TRANSACTION_ID_OFFSET = 8;
    private static final int COOKIE = 0x2112A442;
    private static final short REQUEST = 0x0001;
    private static final short SUCCESS = 0x0101;

    private StunBinding() {
    }

    static byte[] request(SecureRandom random) {
        byte[] transactionId = new byte[HEADER_BYTES - TRANSACTION_ID_OFFSET];
        random.nextBytes(transactionId);
        return ByteBuffer.allocate(HEADER_BYTES)
                .putShort(REQUEST)
                .putShort((short) 0)
                .putInt(COOKIE)
                .put(transactionId)
                .array();
    }

    static boolean matches(byte[] request, byte[] reply, int length) {
        if (length < HEADER_BYTES || length > reply.length || length % 4 != 0) {
            return false;
        }

        ByteBuffer header = ByteBuffer.wrap(reply);
        if (header.getShort() != SUCCESS
                || Short.toUnsignedInt(header.getShort()) != length - HEADER_BYTES
                || header.getInt() != COOKIE) {
            return false;
        }

        for (int i = TRANSACTION_ID_OFFSET; i < HEADER_BYTES; i++) {
            if (request[i] != reply[i]) {
                return false;
            }
        }
        return true;
    }
}
