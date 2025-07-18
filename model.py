import torch
import torch.nn as nn
import math


class InputEmbeddings(nn.Module):

    def __init__(self, d_model: int, vocab_size: int):
        super().__init__()
        self.d_model = d_model
        self.vocab_size = vocab_size
        self.embeddings = nn.Embedding(vocab_size, d_model)

    def forward(self, x):
        return self.embeddings(x) * math.sqrt(self.d_model)


class PositionEncoding(nn.Module):

    def __init__(self, d_model: int, seq_len: int, dropout: float):
        super().__init__()
        self.d_model = d_model
        self.seq_len = seq_len
        self.dropout = nn.Dropout(dropout)

        # Create a matrix of shape (seq_len, d_model)
        pe = torch.zeros(seq_len, d_model)
        # Create a vector of shape (seq_len, 1)
        position = torch.arange(0, seq_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        # Apply the sin to even positions and odd positions (1, seq_len, d_model)
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)

        self.register_buffer(name='pe', tensor=pe)

    def forward(self, x):
        x = x + (self.pe[:, :x.size(1), :]).requires_grad_(False)
        return self.dropout(x)


class LayerNormalization(nn.Module):
    def __init__(self, eps: float = 1e-6):
        super().__init__()
        self.eps = eps
        self.alpha = nn.Parameter(torch.ones(1), requires_grad=True)    # Multiplied
        self.bias = nn.Parameter(torch.zeros(1), requires_grad=True)    # Added

    def forward(self, x):
        mean = x.mean(dim=-1, keepdim=True)
        std = x.std(dim=-1, keepdim=True)
        return self.alpha * (x - mean) / (std + self.eps) + self.bias


class FeedForwardBlock(nn.Module):

    def __init__(self, d_model: int, d_ff: int, dropout: float):
        super().__init__()
        self.linear1 = nn.Linear(in_features=d_model, out_features=d_ff, bias=True)     # W1 and B1
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(in_features=d_ff, out_features=d_model, bias=True)     # W2 and B2

    def forward(self, x):
        # (batch, seq_len, d_model) --> (batch, seq_len, d_ff) --> (batch, seq_len, d_model)
        return self.linear2(self.dropout(torch.relu(self.linear1(x))))


class MultiHeadAttentionBlock(nn.Module):

    def __init__(self, d_model: int, h: int, dropout: float):
        super().__init__()
        self.d_model = d_model
        self.h = h
        assert self.d_model % h == 0, "d_model must be divisible by h"

        self.d_k = d_model // h
        self.w_q = nn.Linear(in_features=d_model, out_features=d_model, bias=True)  # Wq
        self.w_k = nn.Linear(in_features=d_model, out_features=d_model, bias=True)  # Wk
        self.w_v = nn.Linear(in_features=d_model, out_features=d_model, bias=True)  # Wv

        self.w_o = nn.Linear(in_features=d_model, out_features=d_model, bias=True)  # Wo
        self.dropout = nn.Dropout(dropout)

    @staticmethod
    def attention(query, key, value, mask=None, dropout: nn.Dropout = None):
        d_k = query.size(-1)

        # (batch, h, seq_len, d_k) --> (batch, h, seq_len, seq_len)
        attention_scores = (query @ key.transpose(-2, -1)) / math.sqrt(d_k)
        if mask is not None:
            attention_scores.masked_fill_(mask == 0, -1e9)
        attention_scores = attention_scores.softmax(dim=-1)     # (batch, h, seq_len, seq_len)
        if dropout is not None:
            attention_scores = dropout(attention_scores)

        return (attention_scores @ value), attention_scores

    def forward(self, q, k, v, mask):
        query = self.w_q(q)     # (batch, seq_len, d_model) --> (batch, seq_len, d_model)
        key = self.w_k(k)       # (batch, seq_len, d_model) --> (batch, seq_len, d_model)
        value = self.w_v(v)     # (batch, seq_len, d_model) --> (batch, seq_len, d_model)

        # (batch, seq_len, d_model) --> (batch, seq_len, h, d_k) --> (batch, h, seq_len, d_k)
        query = query.view(query.size(0), query.size(1), self.h, self.d_k).transpose(1, 2)
        key = key.view(key.size(0), key.size(1), self.h, self.d_k).transpose(1, 2)
        value = value.view(value.size(0), value.size(1), self.h, self.d_k).transpose(1, 2)

        x, self.attention_scores = MultiHeadAttentionBlock.attention(query, key, value, mask, self.dropout)

        # (batch, h, seq_len, d_k) -- > (batch, seq_len, h, d_k) --> (batch, seq_len, d_model)
        x = x.transpose(1, 2).contiguous().view(x.size(0), -1, self.h * self.d_k)

        # (batch, seq_len, d_model) --> (batch, seq_len, d_model)
        return self.w_o(x)


class ResidualConnection(nn.Module):

    def __init__(self, dropout: float):
        super().__init__()
        self.dropout = nn.Dropout(dropout)
        self.norm = LayerNormalization()

    def forward(self, x, sublayer):
        return x + self.dropout(sublayer(self.norm(x)))


class EncoderBlock(nn.Module):

    def __init__(self, self_attention_block: MultiHeadAttentionBlock, feed_forward_block: FeedForwardBlock,
                 dropout: float):
        super().__init__()
        self.self_attention_block = self_attention_block
        self.feed_forward_block = feed_forward_block
        self.residual_connections = nn.ModuleList([ResidualConnection(dropout) for _ in range(2)])

    def forward(self, x, src_mask):
        x = self.residual_connections[0](x, lambda x: self.self_attention_block(x, x, x, src_mask))
        x = self.residual_connections[1](x, lambda x: self.feed_forward_block(x))
        return x


class Encoder(nn.Module):

    def __init__(self, layers: nn.ModuleList):
        super().__init__()
        self.layers = layers
        self.norm = LayerNormalization()

    def forward(self, x, mask):
        for layer in self.layers:
            x = layer(x, mask)
        return self.norm(x)


class DecoderBlock(nn.Module):

    def __init__(self, self_attention_block: MultiHeadAttentionBlock, cross_attention_block: MultiHeadAttentionBlock,
                 feed_forward_block: FeedForwardBlock, dropout: float):
        super().__init__()
        self.self_attention_block = self_attention_block
        self.cross_attention_block = cross_attention_block
        self.feed_forward_block = feed_forward_block
        self.residual_connections = nn.ModuleList([ResidualConnection(dropout) for _ in range(3)])

    def forward(self, x, encoder_output, src_mask, tgt_mask):
        x = self.residual_connections[0](x, lambda x: self.self_attention_block(x, x, x, tgt_mask))
        x = self.residual_connections[1](x, lambda x: self.cross_attention_block(x, encoder_output, encoder_output, src_mask))
        x = self.residual_connections[2](x, lambda x: self.feed_forward_block(x))
        return x


class Decoder(nn.Module):

    def __init__(self, layers: nn.ModuleList):
        super().__init__()
        self.layers = layers
        self.norm = LayerNormalization()

    def forward(self, x, encoder_output, src_mask, tgt_mask):
        for layer in self.layers:
            x = layer(x, encoder_output, src_mask, tgt_mask)
        return self.norm(x)


class ProjectionLayer(nn.Module):

    def __init__(self, d_model: int, vocab_size: int):
        super().__init__()
        self.proj = nn.Linear(in_features=d_model, out_features=vocab_size)

    def forward(self, x):
        # (batch, seq_len, d_model) --> (batch,  seq_len, vocab_size)
        return torch.log_softmax(self.proj(x), dim=-1)


class Transformer(nn.Module):

    def __init__(self, encoder: Encoder, decoder: Decoder, src_embed: InputEmbeddings, tgt_embed: InputEmbeddings,
                 src_pos: PositionEncoding, tgt_pos: PositionEncoding, projection_layer: ProjectionLayer):
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.src_embed = src_embed
        self.tgt_embed = tgt_embed
        self.src_pos = src_pos
        self.tgt_pos = tgt_pos
        self.projection_layer = projection_layer

    def encode(self, src, src_mask):
        src = self.src_embed(src)
        src = self.src_pos(src)
        return self.encoder(src, src_mask)

    def decode(self, encoder_output, src_mask, tgt, tgt_mask):
        tgt = self.tgt_embed(tgt)
        tgt = self.tgt_pos(tgt)
        return self.decoder(tgt, encoder_output, src_mask, tgt_mask)

    def project(self, x):
        return self.projection_layer(x)


def build_transformer(src_vocab_size: int, tgt_vocab_size: int, src_seq_len: int, tgt_seq_len: int, d_model: int = 512,
                      N: int = 6, h: int = 8, d_ff: int = 2048, dropout: float = 0.1) -> Transformer:
    # Create the embedding layers
    src_embed = InputEmbeddings(d_model=d_model, vocab_size=src_vocab_size)
    tgt_embed = InputEmbeddings(d_model=d_model, vocab_size=tgt_vocab_size)

    # Create the positional encoding layers
    src_pos = PositionEncoding(d_model=d_model, seq_len=src_seq_len, dropout=dropout)
    tgt_pos = PositionEncoding(d_model=d_model, seq_len=tgt_seq_len, dropout=dropout)

    # Create the encoder blocks
    encoder_blocks = []
    for _ in range(N):
        encoder_self_attention_block = MultiHeadAttentionBlock(d_model=d_model, h=h, dropout=dropout)
        encoder_feed_forward_block = FeedForwardBlock(d_model=d_model, d_ff=d_ff, dropout=dropout)
        encoder_block = EncoderBlock(encoder_self_attention_block, encoder_feed_forward_block, dropout=dropout)
        encoder_blocks.append(encoder_block)

    # Create the decoder blocks
    decoder_blocks = []
    for _ in range(N):
        decoder_self_attention_block = MultiHeadAttentionBlock(d_model=d_model, h=h, dropout=dropout)
        decoder_cross_attention_block = MultiHeadAttentionBlock(d_model=d_model, h=h, dropout=dropout)
        decoder_feed_forward_block = FeedForwardBlock(d_model=d_model, d_ff=d_ff, dropout=dropout)
        decoder_block = DecoderBlock(decoder_self_attention_block, decoder_cross_attention_block,
                                     decoder_feed_forward_block, dropout=dropout)
        decoder_blocks.append(decoder_block)

    # Create the encoder and decoder
    encoder = Encoder(nn.ModuleList(encoder_blocks))
    decoder = Decoder(nn.ModuleList(decoder_blocks))

    # Create the projection layer
    projection_layer = ProjectionLayer(d_model=d_model, vocab_size=tgt_vocab_size)

    # Create the transformer
    transformer = Transformer(encoder, decoder, src_embed, tgt_embed, src_pos, tgt_pos, projection_layer)

    # Initialize the parameters
    for p in transformer.parameters():
        if p.dim() > 1:
            nn.init.xavier_uniform_(p)

    return transformer


if __name__ == '__main__':
    d_model = 512
    d_ff = 2048
    seq_len = 8
    dropout = 0.1
    vocab_size = 20000

    model = build_transformer(src_vocab_size=vocab_size, tgt_vocab_size=vocab_size, src_seq_len=seq_len,
                              tgt_seq_len=seq_len, d_model=d_model, d_ff=d_ff, dropout=dropout)
    print(model)
