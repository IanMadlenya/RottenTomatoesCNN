import os
import time
import datetime

import numpy as np
import tensorflow as tf
from tensorflow.contrib import learn

import data
from text_similarity_cnn import TextSimilarityCNN


# Parameters
# ==================================================

# Data parameters
test_sample_percentage = 0.1  # percentage of training data to use for testing
positive_data_file = "data/rt-polarity.pos"
negative_data_file = "data/rt-polarity.neg"

# Pre-trained word embeddings
embedding_dim = 300  # dimensionality of embedding
embedding_file = "data/GoogleNews-vectors-negative300.bin"  # word embeddings file

# Model parameters
filter_heights = "3,4,5"  # comma-separated filter heights
num_features = 128  # number of features per filter

# Training parameters
learning_rate = 1e-3
batch_size = 64
num_epochs = 200

# Regularization parameters
dropout_keep_prob = 0.5  # dropout keep probability
l2_reg_lambda = 0.0  # L2 regularization lambda

# Misc. parameters
evaluate_every = 100  # evaluate model on test set after this many steps
checkpoint_every = 100  # save model after this many steps
num_checkpoints = 5  # number of checkpoints to store

allow_soft_placement = True  # allow device soft device placement i.e. fall back on available device
log_device_placement = False  # log placement of operations on devices


# Data Preparation
# ==================================================

# Load data
print("Loading data...")
x_text, y = data.load_data_and_labels(positive_data_file, negative_data_file)

# Build vocabulary
max_document_length = max([len(x.split(" ")) for x in x_text])
vocab_processor = learn.preprocessing.VocabularyProcessor(max_document_length)
x = np.array(list(vocab_processor.fit_transform(x_text)))
print("Max. Sentence Length: {}".format(max_document_length))

# Randomly shuffle data
np.random.seed(10)
shuffle_indices = np.random.permutation(np.arange(len(y)))
x_shuffled = x[shuffle_indices]
y_shuffled = y[shuffle_indices]

# Split train/test set
test_sample_index = -1 * int(test_sample_percentage * float(len(y)))
x_train, x_test = x_shuffled[:test_sample_index], x_shuffled[test_sample_index:]
y_train, y_test = y_shuffled[:test_sample_index], y_shuffled[test_sample_index:]

del x, y, x_shuffled, y_shuffled  # don't need these anymore

print("Vocabulary Size: {}".format(len(vocab_processor.vocabulary_)))
print("Train/Test Split: {}/{}".format(len(y_train), len(y_test)))

# Process Google News word2vec file (in a memory-friendly way) and store relevant embeddings.
print("Loading pre-trained embeddings from {}...".format(embedding_file))
embeddings = data.load_word2vec(embedding_file, vocab_processor.vocabulary_, embedding_dim, tf_VP=True)


# Training
# ==================================================

with tf.Graph().as_default():
    session_conf = tf.ConfigProto(allow_soft_placement=allow_soft_placement,
                                  log_device_placement=log_device_placement)
    sess = tf.Session(config=session_conf)
    with sess.as_default():
        cnn = TextSimilarityCNN(sequence_length=x_train.shape[1],
                                num_classes=y_train.shape[1],
                                vocab_size=len(vocab_processor.vocabulary_),
                                embedding_size=embedding_dim,
                                embeddings=embeddings,
                                filter_heights=list(map(int, filter_heights.split(","))),
                                num_features=num_features,
                                l2_reg_lambda=l2_reg_lambda)

        # Define training procedure
        global_step = tf.Variable(0, name="global_step", trainable=False)
        optimizer = tf.train.AdamOptimizer(learning_rate)
        grads_and_vars = optimizer.compute_gradients(cnn.loss)
        train_op = optimizer.apply_gradients(grads_and_vars, global_step=global_step)

        # Keep track of gradient values and sparsity
        grad_summaries = []
        for g, v in grads_and_vars:
            if g is not None:
                grad_hist_summary = tf.summary.histogram("{}/grad/hist".format(v.name), g)
                sparsity_summary = tf.summary.scalar("{}/grad/sparsity".format(v.name), tf.nn.zero_fraction(g))
                grad_summaries.append(grad_hist_summary)
                grad_summaries.append(sparsity_summary)
        grad_summaries_merged = tf.summary.merge(grad_summaries)

        # Output directory for models and summaries
        timestamp = str(int(time.time()))
        out_dir = os.path.abspath(os.path.join(os.path.curdir, "runs", "v2", timestamp))
        print("Writing to {}\n".format(out_dir))

        # Summaries for loss and accuracy
        loss_summary = tf.summary.scalar("loss", cnn.loss)
        acc_summary = tf.summary.scalar("accuracy", cnn.accuracy)

        # Train Summaries
        train_summary_op = tf.summary.merge([loss_summary, acc_summary, grad_summaries_merged])
        train_summary_dir = os.path.join(out_dir, "summaries", "train")
        train_summary_writer = tf.summary.FileWriter(train_summary_dir, sess.graph)

        # Test Summaries
        test_summary_op = tf.summary.merge([loss_summary, acc_summary])
        test_summary_dir = os.path.join(out_dir, "summaries", "test")
        test_summary_writer = tf.summary.FileWriter(test_summary_dir, sess.graph)

        # Checkpoint directory & saver
        checkpoint_dir = os.path.abspath(os.path.join(out_dir, "checkpoints"))
        checkpoint_prefix = os.path.join(checkpoint_dir, "model")
        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)
        saver = tf.train.Saver(tf.global_variables(), max_to_keep=num_checkpoints)

        # Write vocabulary
        vocab_processor.save(os.path.join(out_dir, "vocab"))

        # Initialize all variables
        sess.run(tf.global_variables_initializer())

        def train_step(x_batch, y_batch):
            """
            A single training step.
            """
            feed_dict = {
                cnn.input_x: x_batch,
                cnn.input_y: y_batch,
                cnn.dropout_keep_prob: dropout_keep_prob
            }
            _, step, summaries, loss, accuracy = sess.run([train_op, global_step, train_summary_op, cnn.loss,
                                                           cnn.accuracy],
                                                          feed_dict)
            time_str = datetime.datetime.now().isoformat()
            print("{}: Step {}, Loss {:g}, Accuracy {:g}".format(time_str, step, loss, accuracy))
            train_summary_writer.add_summary(summaries, step)

        def test_step(x_batch, y_batch, writer=None):
            """
            Evaluates model on a test set.
            """
            feed_dict = {
                cnn.input_x: x_batch,
                cnn.input_y: y_batch,
                cnn.dropout_keep_prob: 1.0
            }
            step, summaries, loss, accuracy = sess.run([global_step, test_summary_op, cnn.loss,
                                                        cnn.accuracy],
                                                       feed_dict)
            time_str = datetime.datetime.now().isoformat()
            print("{}: Step {}, Loss {:g}, Accuracy {:g}".format(time_str, step, loss, accuracy))
            if writer:
                writer.add_summary(summaries, step)
            return accuracy

        # Generate batches
        batches = data.batch_iter(list(zip(x_train, y_train)), batch_size, num_epochs)

        # Maximum test accuracy
        max_accuracy = 0

        # Training loop
        for batch in batches:
            x_batch, y_batch = zip(*batch)
            train_step(x_batch, y_batch)
            current_step = tf.train.global_step(sess, global_step)
            if current_step % evaluate_every == 0:
                print("\nEvaluation:")
                accuracy = test_step(x_test, y_test, writer=test_summary_writer)
                if accuracy > max_accuracy:
                    max_accuracy = accuracy
                print("Max. Test Accuracy: {:g}".format(max_accuracy))
                print("")
            if current_step % checkpoint_every == 0:
                path = saver.save(sess, checkpoint_prefix, global_step=current_step)
                print("Saved model checkpoint to {}\n".format(path))
