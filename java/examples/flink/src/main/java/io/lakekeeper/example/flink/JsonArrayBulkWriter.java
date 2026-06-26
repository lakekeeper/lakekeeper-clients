package io.lakekeeper.example.flink;

import org.apache.flink.api.common.serialization.BulkWriter;
import org.apache.flink.core.fs.FSDataOutputStream;

import java.io.IOException;
import java.nio.charset.StandardCharsets;

/**
 * Writes a rolling file as a proper JSON array: [{...},{...},...].
 * Each file starts with "[\n", records are comma-separated, and the file ends with "\n]".
 */
final class JsonArrayBulkWriter implements BulkWriter<String> {

    private final FSDataOutputStream out;
    private boolean first = true;

    private JsonArrayBulkWriter(FSDataOutputStream out) throws IOException {
        this.out = out;
        out.write("[\n".getBytes(StandardCharsets.UTF_8));
    }

    @Override
    public void addElement(String element) throws IOException {
        if (!first) out.write(",\n".getBytes(StandardCharsets.UTF_8));
        out.write(element.getBytes(StandardCharsets.UTF_8));
        first = false;
    }

    @Override
    public void flush() throws IOException {
        out.flush();
    }

    @Override
    public void finish() throws IOException {
        out.write("\n]".getBytes(StandardCharsets.UTF_8));
        out.flush();
    }

    static BulkWriter.Factory<String> factory() {
        return JsonArrayBulkWriter::new;
    }
}
