"""Hand-written overviews for the demo papers. Shown in Paper Understanding as "curated" (never passed off as model output), and
preferred over the model's summary for these papers so the demo reads the same every time. Matching is by title or file name."""
from __future__ import annotations

from typing import Any

CURATED: list[dict[str, Any]] = [
    {
        "match": ("attention is all you need", "1706.03762"),
        "summary": (
            "Sequence models for translation had relied on recurrent or convolutional networks that read text step by step, which is slow "
            "to train and struggles with long-range dependencies. This paper introduces the Transformer, a model built only from attention: "
            "every word looks directly at every other word to decide what matters. Because nothing has to be processed in order, training "
            "parallelises well and is much faster. On WMT 2014 English-to-German the big model reaches 28.4 BLEU, more than 2 BLEU above the "
            "best earlier systems, and on English-to-French it reaches 41.8 BLEU after 3.5 days on eight GPUs, at a fraction of the earlier "
            "training cost."
        ),
        "method": (
            "An encoder-decoder network of 6 layers each. Every layer uses multi-head self-attention (8 heads, model width 512) followed by "
            "a small feed-forward network, with residual connections and layer normalisation. Since there is no recurrence, sine/cosine "
            "positional encodings tell the model word order. It is trained with the Adam optimiser using a warm-up then decaying learning "
            "rate, dropout of 0.1 and label smoothing of 0.1."
        ),
    },
    {
        "match": ("mixup", "1710.09412"),
        "summary": (
            "Large neural networks tend to memorise their training data and are easily fooled by slightly altered inputs. mixup is a very "
            "simple fix: instead of training on single examples, train on blends of pairs of examples and blend their labels the same way. "
            "This makes the network behave more smoothly between training points. Across ImageNet, CIFAR-10 and CIFAR-100 it lowers test "
            "error for a range of architectures (for example CIFAR-10 error drops from 5.6% to 4.2% for PreAct ResNet-18), makes models "
            "more robust to corrupted labels and adversarial examples, and stabilises the training of GANs."
        ),
        "method": (
            "For each training batch, pick a random blend weight λ from a Beta(α, α) distribution (α = 0.2 for ImageNet, 1.0 for CIFAR), "
            "shuffle the batch, and train on x = λ·x_i + (1−λ)·x_j with target y = λ·y_i + (1−λ)·y_j. The loss is the usual cross-entropy "
            "on these blended pairs. It needs only a few lines of code, no extra parameters, and negligible extra compute."
        ),
    },
    {
        "match": ("deep residual learning on cifar-10", "resnet-demo", "demo.pdf"),
        "summary": (
            "A one-page experiment note: a ResNet-20 (a small residual network) is trained on the CIFAR-10 image-classification dataset and "
            "is reported to reach 91.25% top-1 accuracy on the test set. The note gives the full training recipe and a code link, so the "
            "result can be re-run and checked, which makes it a compact example for reproducibility checking."
        ),
        "method": (
            "ResNet-20 trained with SGD (momentum 0.9), learning rate 0.1, batch size 128, for 160 epochs, with random seed 42. The authors' "
            "code is at github.com/acme/resnet-cifar."
        ),
    },
]


def curated_for(*names: str | None) -> dict[str, str] | None:
    hay = " ".join(n.lower() for n in names if n)
    for c in CURATED:
        if any(m in hay for m in c["match"]):
            return {"summary": c["summary"], "method": c["method"], "source": "curated"}
    return None
