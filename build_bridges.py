

import os
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
import wandb
import numpy as np
import tensorflow as tf
tf.compat.v1.disable_eager_execution()
import tensorflow_probability as tfp
tfb = tfp.bijectors
tfd = tfp.distributions

from gauss_1d_analysis import analyse_objective_fn_for_1d_gauss, analyse_objective_for_1d_gauss_multiple_sample_sizes
from experiment_ops import build_energies, plot_per_ratio_and_datapoint_diagnostics, load_model, load_flow
from losses import LogisticLoss, NWJLoss, LSQLoss
from sklearn.metrics import roc_auc_score
from utils.misc_utils import *
from utils.tf_utils import *
from utils.experiment_utils import *
from utils.plot_utils import *
from waymark_ops import *


def get_dimwise_mixing_ordering_event_shape(event_shp, config):
    if config.do_mutual_info_estimation:
        event_shp = event_shp[:-1]
    if config.n_event_dims_to_mix is not None:
        event_shp = event_shp[-config.n_event_dims_to_mix:]
    if config.dataset_name == "multiomniglot":
        event_shp = [config.data_args["n_imgs"]]
    return event_shp


# noinspection PyUnresolvedReferences
def build_placeholders(config):

    if ("img_shape" in config.data_args) and (config.data_args["img_shape"] is not None):
        order_shp = get_dimwise_mixing_ordering_event_shape(config.data_args["img_shape"], config)
        dimwise_mixing_ordering = tf.compat.v1.placeholder(tf.int32, (None, *order_shp), "dimwise_mixing_ordering")
        data = tf.compat.v1.placeholder(tf.float32, (None, *config.data_args["img_shape"]), "data")
    else:
        dimwise_mixing_ordering = tf.compat.v1.placeholder(tf.int32, (None, config.n_dims), "dimwise_mixing_ordering")
        data = tf.compat.v1.placeholder(tf.float32, (None, config.n_dims), "data")

    # these two index variables are redundant since bridge_idxs = waymark_idxs[:-1] by default
    # I separated them out during some experiments where I was adding/removing waymarks during learning
    # and haven't yet removed one of them
    waymark_idxs = tf.compat.v1.placeholder(tf.int32, (None, ), name="waymark_idxs")
    bridge_idxs = tf.compat.v1.placeholder(tf.int32, (None, ), name="bridge_idxs")

    head_multiplier = tf.compat.v1.placeholder_with_default(1.0, (), name="head_multiplier")
    lr_var = tf.compat.v1.placeholder_with_default(1e-4, (), name="learning_rate")
    scale_lr_multiplier = tf.compat.v1.placeholder_with_default(config.scale_param_lr_multiplier, (), name="scale_lr_multiplier")
    loss_weights = tf.compat.v1.placeholder(tf.float32, (None, ), name="loss_weights")

    return AttrDict(locals())


# noinspection PyUnresolvedReferences
def build_optimisers(tre_loss, logistic_tre_loss_term1,logistic_tre_loss_term2,pholders,max_num_ratios, config):
    """Optimise energy-based model parameters"""

    model_scope = "tre_model"
    scale_param_lr = pholders.lr_var * pholders.scale_lr_multiplier

    optimizer, scale_optimizer = build_optimiser(config, pholders.lr_var, scale_param_lr)

    energy_params = tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.TRAINABLE_VARIABLES, scope=model_scope)
    scale_param = [v for v in energy_params if "b_all" in v.name][0]
    energy_params = [v for v in energy_params if "b_all" not in v.name]

    if config.use_bar_update:
        # grad=None
        # for index in range(config.num_losses):
        #     loss_term=tf.reduce_sum(logistic_tre_loss_term1[:,index])
        #     if grad is None:
        #         grad=scale_optimizer.compute_gradients(loss_term, var_list=scale_param)
        #     else:
        #         grad=grad+scale_optimizer.compute_gradients(loss_term, var_list=scale_param)       
        # for index in range(config.num_losses):
        #     loss_term=tf.reduce_sum(logistic_tre_loss_term2[:,index])
        #     grad=grad+scale_optimizer.compute_gradients(loss_term, var_list=scale_param)
        grad=scale_optimizer.compute_gradients(logistic_tre_loss_term1, var_list=scale_param)
        grad=grad+scale_optimizer.compute_gradients(logistic_tre_loss_term2, var_list=scale_param)
    else:
        grad=scale_optimizer.compute_gradients(tf.reduce_mean(tre_loss), var_list=scale_param)
    grad=[(tf.clip_by_value(g, -10, 10), v) for g, v in grad]
    scale_optim_op = scale_optimizer.apply_gradients(grad)
    reg_term = tf.compat.v1.losses.get_regularization_loss(scope=model_scope)
    tre_optim_op = optimizer.minimize(tf.reduce_mean(tre_loss) + reg_term, var_list=energy_params)

    optim_op = tf.group(tre_optim_op, scale_optim_op)

    return optim_op


def build_optimiser(config, lr_var, scale_param_lr):

    if "scale_param_lr" in config.keys():
        scale_param_lr = config.scale_param_lr
    if config.optimizer == "adam":
        optimizer = tf.compat.v1.train.AdamOptimizer(lr_var)
        scale_optimizer = tf.compat.v1.train.AdamOptimizer(scale_param_lr)
    elif config.optimizer == "lazy_adam":
        optimizer = tf.compat.v1.contrib.opt.LazyAdamOptimizer(lr_var)
        scale_optimizer = tf.compat.v1.contrib.opt.LazyAdamOptimizer(scale_param_lr)
    elif config.optimizer == "rmsprop":
        optimizer = tf.compat.v1.train.RMSPropOptimizer(lr_var)
        scale_optimizer = tf.compat.v1.train.RMSPropOptimizer(scale_param_lr)
    elif config.optimizer == "momentum":
        optimizer = tf.compat.v1.train.MomentumOptimizer(lr_var, momentum=0.9, use_nesterov=True)
        scale_optimizer = tf.compat.v1.train.MomentumOptimizer(scale_param_lr, momentum=0.9, use_nesterov=True)
    else:
        raise ValueError("unknown optimizer: {}".format(config.optimizer))
    if config.use_bar_update:
        scale_optimizer = tf.compat.v1.train.GradientDescentOptimizer(scale_param_lr)
        #scale_optimizer = tf.compat.v1.train.MomentumOptimizer(scale_param_lr, momentum=0.9, use_nesterov=True)
        #scale_optimizer = tf.compat.v1.train.AdamOptimizer(scale_param_lr)
    return optimizer, scale_optimizer


def build_train_loss(config, neg_energy, loss_weights):

    logistic_obj = LogisticLoss(tf.constant(config.objective_nu, dtype=tf.float32),
                                label_smoothing_alpha=config.get("label_smoothing_alpha", 0.0),
                                one_sided_smoothing=config.get("one_sided_smoothing", True)
                                )
    logistic_tre_loss, logistic_tre_loss_term1, logistic_tre_loss_term2 = logistic_obj.loss(neg_energy)  # (n_losses, )

    nwj_object = NWJLoss()
    nwj_tre_loss, _, _ = nwj_object.loss(neg_energy)

    lsq_loss_obj = LSQLoss()
    lsq_tre_loss, _, _ = lsq_loss_obj.loss(neg_energy)

    if config.loss_function == "logistic":
        tre_train_loss = logistic_tre_loss
    elif config.loss_function == "nwj":
        tre_train_loss = nwj_tre_loss
    elif config.loss_function == "lsq":
        tre_train_loss = lsq_tre_loss
    else:
        raise ValueError("did not recognise loss function type {}".format(config.loss_function))

    tre_train_loss = tre_train_loss * loss_weights  # default weights are uniform

    return tre_train_loss,logistic_tre_loss_term1,logistic_tre_loss_term2


def build_val_loss(config, val_neg_energies):

    # todo: unnecessary duplication of code from build_train_loss.

    logistic_loss_obj = LogisticLoss(tf.constant(config.objective_nu, dtype=tf.float32),
                                     label_smoothing_alpha=config.get("label_smoothing_alpha", 0.0),
                                     one_sided_smoothing=config.get("one_sided_smoothing", True)
                                     )
    logistic_loss_op, logistic_term1, logistic_term2 = logistic_loss_obj.loss(val_neg_energies)  # (n_losses, ), (n, n_losses)*2

    nwj_loss_obj = NWJLoss()
    nwj_loss_op, nwj_term1, nwj_term2 = nwj_loss_obj.loss(val_neg_energies)
    lsq_loss_obj = LSQLoss()
    lsq_loss_op, lsq_term1, lsq_term2 = lsq_loss_obj.loss(val_neg_energies)

    if config.loss_function == "logistic":
        loss_obj = logistic_loss_obj
        val_loss, term1, term2 = logistic_loss_op, logistic_term1, logistic_term2
    elif config.loss_function == "nwj":
        loss_obj = nwj_loss_obj
        val_loss, term1, term2 = nwj_loss_op, nwj_term1, nwj_term2
    elif config.loss_function == "lsq":
        loss_obj = lsq_loss_obj
        val_loss, term1, term2 = lsq_loss_op, lsq_term1, lsq_term2
    else:
        raise ValueError("did not recognise loss function type {}".format(config.loss_function))

    loss_terms = [term1, term2]

    return loss_obj, val_loss, loss_terms, nwj_loss_op


# noinspection PyUnresolvedReferences
def build_graph(config):
    """Build graph for executing telescoping ratio estimation

    Returns: dictionary of of all local variables, including the graph ops required for training
    """
    # placeholders
    pholders = build_placeholders(config)

    waymark_construction_results = tf_get_waymark_data(config, pholders)
    wmark0_data = waymark_construction_results.waymark0_data
    wmark_data = waymark_construction_results.waymark_data

    with tf.compat.v1.variable_scope("tre_model"):

        idxs = config.initial_waymark_indices
        max_num_ratios = idxs[-1]
        print("max num ratios is: ", max_num_ratios)

        energy_obj = build_energies(config=config,
                                    bridge_idxs=pholders.bridge_idxs,
                                    max_num_ratios=max_num_ratios,
                                    head_multiplier=pholders.head_multiplier
                                    )

        neg_energies = energy_obj.neg_energy(wmark_data, is_train=True, is_wmark_input=True)

    # build train loss & optimisation step
    tre_train_loss,logistic_tre_loss_term1,logistic_tre_loss_term2 = build_train_loss(config, neg_energies, pholders.loss_weights)
    tre_optim_op = build_optimisers(tre_train_loss, logistic_tre_loss_term1,logistic_tre_loss_term2,pholders, max_num_ratios,config)

    # build validation operations
    val_neg_energies = energy_obj.neg_energy(wmark_data, is_train=False, is_wmark_input=True)
    loss_obj, val_loss, loss_terms, nwj_loss_op = build_val_loss(config, val_neg_energies)

    neg_energies_of_data = energy_obj.neg_energy(wmark0_data, is_train=False, is_wmark_input=False)  # (n_batch, n_ratios)
    av_neg_energies_of_data = tf.reduce_mean(neg_energies_of_data, axis=0)  # (n_ratios, )

    if "2d" in config.dataset_name or "1d" in config.dataset_name:
        noise_logprob = waymark_construction_results.noise_dist.log_prob(wmark0_data)
        bridges_and_noise_neg_e_of_data = tf.concat([neg_energies_of_data, tf.expand_dims(noise_logprob, axis=1)], axis=1)

    spec_norms = []
    if hasattr(energy_obj, "model"):
        for layer in energy_obj.model.layers:
            if hasattr(layer, "spectral_norm"):
                spec_norms.append(layer.spectral_norm)

    average_metric_ops = [
        loss_obj.acc,
        loss_obj.class1_acc,
        loss_obj.class2_acc,
        loss_obj.dawid_statistic_numerator,
        loss_obj.dawid_statistic_denominator,
        val_loss,
        nwj_loss_op,
        av_neg_energies_of_data
    ]

    graph = AttrDict(locals())
    graph.update(pholders)
    return graph   # dict whose values can be accessed as attributes i.e. val = dict.key


# noinspection PyUnresolvedReferences,PyUnboundLocalVariable
def train(g, sess, train_dp, val_dp, saver1, saver2, config):
    """Train ratio-estimators"""

    logger = logging.getLogger("tf")
    model_dir = "{}model/".format(config.save_dir)
    os.makedirs(model_dir, exist_ok=True)

    start_epoch_idx = config.get("epoch_idx", -1)
    config["epoch_idx"] = start_epoch_idx

    n_batches_seen = 0
    config["n_epochs_until_stop"], config["best_val_loss"] = config.patience, np.inf

    for _ in range(start_epoch_idx, config.n_epochs):

        learn_rate, train_dp, val_dp = pre_epoch_events(config, train_dp, val_dp, logger)

        for j, batch in enumerate(train_dp):

            fd = get_feed_dict(g, sess, train_dp, batch, config, lr=learn_rate, j=j)
            tre_train_loss,_=sess.run([g.tre_train_loss,g.tre_optim_op], feed_dict=fd)
            wandb.log({"tre_train_loss":tf.reduce_mean(tre_train_loss)})
            n_batches_seen += 1

        stop = post_epoch_events(g, sess, train_dp, val_dp, saver1, saver2, model_dir, config, logger)
        if stop:
            if config.save_every_x_epochs:
                saver1.save(sess, os.path.join(model_dir, "every_x_epochs/{}.ckpt".format(config.epoch_idx)))
            break  # early stopping triggered

        config.n_epochs_until_stop -= 1

    logger.info("Finished training model!")
    logger.info("Ratios were estimated for the following datasets: {}".format(config.initial_waymark_indices))

    # restore and eval best model found via early stopping
    saver2.restore(sess, tf.train.latest_checkpoint(model_dir))
    post_learning_summary(g, sess, train_dp, val_dp, config)


def get_feed_dict(g, sess, dp, batch, config, lr=-1, j=-1, train=True):

    waymark_idxs, bridge_idxs = get_waymark_and_bridge_idxs_for_epoch_i(config, j)

    # if doing MI-estimation, randomly sample negative samples from whole dataset
    if config.do_mutual_info_estimation:
        neg_sample_batch = dp.get_rand_batch(batch_size=len(batch))
        batch = np.concatenate([batch, neg_sample_batch], axis=0)

    feed_dict = {g.data: batch,
                 g.waymark_idxs: waymark_idxs,
                 g.bridge_idxs: bridge_idxs,
                 g.loss_weights: np.array([config.loss_decay_factor**i for i in range(config.num_losses)][::-1])
                 }
    if train:
        feed_dict.update({g.lr_var: lr})

    if config.waymark_mechanism == "dimwise_mixing":
        feed_dict[g.dimwise_mixing_ordering] = get_batch_dimwise_mixing_ordering(batch.shape, config)

    if train and j == 0:
        if "noise_multipliers" in g.waymark_construction_results:
            print("waymark coefficients are: ",
                  sess.run(g.waymark_construction_results.noise_multipliers, feed_dict=feed_dict))

    if train and config.epoch_idx == 0 and j == 0:
        plot_waymark_diagnostic_figs(sess, g, waymark_idxs, bridge_idxs, feed_dict, dp, config)

    return feed_dict


# noinspection PyUnresolvedReferences
def pre_epoch_events(config, train_dp, val_dp, logger):

    config["epoch_idx"] += 1

    # adjust learning rate
    lr = 0.5 * config.energy_lr * (1 + np.cos((config.epoch_idx / config.n_epochs) * np.pi))
    logger.info("LEARNING RATE IS NOW {}.".format(lr))

    if not config.shuffle_waymarks:
        batch_size, n_waymarks = config.n_batch, len(config.initial_waymark_indices)
        batch_size -= np.mod(batch_size, n_waymarks)
        train_dp.batch_size = val_dp.batch_size = int(batch_size / n_waymarks)
    logger.info("batch size is: {}".format(train_dp.batch_size))

    return lr, train_dp, val_dp


def post_epoch_events(g, sess, train_dp, val_dp, saver1, saver2, model_dir, config, logger):

    # Evaluate model
    eval_model(g, sess, train_dp, val_dp, config, use_train_data=True, max_num_batch=500)

    # Check early stopping criterion
    save_path = model_dir + "{}.ckpt".format(config.epoch_idx)
    stop, is_best_epoch_so_far = check_early_stopping(saver2, sess, save_path, config)
    if is_best_epoch_so_far:
        logger.info(" " * 60 + "saving results from epoch {} as best".format(config.epoch_idx))
        save_best_results(config)

    save_path = os.path.join(model_dir, "every_x_epochs/{}.ckpt".format(config.epoch_idx))
    if config.save_every_x_epochs and \
            config.epoch_idx > 0 and \
            config.epoch_idx % config.save_every_x_epochs == 0:
        saver1.save(sess, save_path)

    wait_interval = 20 if os.path.isdir(local_pc_root) else 100
    if config.epoch_idx % wait_interval == 0 and \
            ("2d" in config.dataset_name or "1d" in config.dataset_name):
            save_lowdim_energies(g, sess, config, val_dp)

    return stop


# noinspection PyUnresolvedReferences
def post_learning_summary(g, sess, train_dp, val_dp, config):

    if "2d" in config.dataset_name or "1d" in config.dataset_name:
        save_lowdim_energies(g, sess, config, val_dp)

    # saver.restore(sess, tf.train.latest_checkpoint(model_dir))
    eval_model(g, sess, train_dp, val_dp, config, use_train_data=True, save=False)

    # For each bridge, plot histograms of its energies and each term of the corresponding loss function
    fig_dir = os.path.join(config.save_dir, "figs/")

    ops = [g.neg_energies_of_data, g.loss_terms[0], g.loss_terms[1]]
    names = ["neg_e", "first_term_of_loss", "second_term_of_loss"]
    for op, name in zip(ops, names):

        def _feed_dict_fn(j, n, b):
            batch = val_dp.data[j:min(j+b, n), ...]
            return get_feed_dict(g, sess, val_dp, batch, config, train=False)

        plot_per_ratio_and_datapoint_diagnostics(sess=sess,
                                                 metric_op=op,
                                                 num_ratios=len(config.initial_waymark_indices)-1,
                                                 datasets=[val_dp.data],
                                                 data_splits=["val"],
                                                 save_dir=fig_dir,
                                                 dp=val_dp,
                                                 config=config,
                                                 name=name,
                                                 feed_dict_fn=_feed_dict_fn
                                                 )


# noinspection PyUnresolvedReferences,PyTypeChecker
def eval_model(g, sess, train_dp, val_dp, config, use_train_data=False, save=True, max_num_batch=None):
    """Each epoch, evaluate the TRE loss, accuracies & average energy on train+val set"""

    # only use a subset of data to evaluate model
    if max_num_batch:
        assert train_dp.max_num_batches == -1, "waymark dataprovider has max batch size different from -1"
        train_dp.max_num_batches = val_dp.max_num_batches = max_num_batch

    epoch = config.epoch_idx if save else "best"
    logger = logging.getLogger("tf")
    logger.info("------------------------------")
    logger.info("Epoch {}".format(epoch))
    logger.info("------------------------------")

    if use_train_data:
        eval_train_or_val_set(g, sess, train_dp, save, logger, "train", config)

    val_tre_loss = eval_train_or_val_set(g, sess, val_dp, save, logger, "val", config)

    # reset number of batches in data providers
    train_dp.max_num_batches = -1
    val_dp.max_num_batches = -1

    config["current_val_loss"] = np.mean(val_tre_loss)
    wandb.log({"current_val_loss":config["current_val_loss"]})
    wandb.log({"epoch":config.epoch_idx})

def eval_train_or_val_set(g, sess, dp, save, logger, which_set, config):

    # For each ratio, calculate a variety of metrics that are functions of the data
    results = eval_data_dep_metrics(g, sess, dp, config)

    acc, class1_acc, class2_acc, dawid_numer, dawid_denom, tre_loss, nwj_loss, energy = results

    dawid_statistic = dawid_numer / dawid_denom ** 0.5
    spec_norms = np.array(sess.run(g.spec_norms))

    if save:
        waymark_idxs = np.array(config.initial_waymark_indices)
        metrics_save_dir = get_metrics_data_dir(config.save_dir, epoch_i=config.epoch_idx)
        np.savez(os.path.join(metrics_save_dir, which_set),
                 acc=acc,
                 class1_acc=class1_acc,
                 class2_acc=class2_acc,
                 dawid_statistic=dawid_statistic,
                 loss=tre_loss,
                 nwj_loss=nwj_loss,
                 energy=energy,
                 waymark_idxs=waymark_idxs,
                 spec_norms=spec_norms
                 )

    logger.info("{} tre loss {:0.3f}".format(which_set, np.mean(tre_loss)))
    logger.info("{} total neg energies  {:0.3f}".format(which_set, np.sum(energy)))

    wandb.log({f"{which_set}_epoch_tre_loss":np.mean(tre_loss),f"{which_set}_epoch_total_neg_energy":np.sum(energy)})

    logger.info("\n{} tre losses {}".format(which_set, tre_loss))
    logger.info("\n{} neg energies {}".format(which_set, energy))

    count=0
    for loss,energy in zip(tre_loss,energy):
        wandb.log({f"{which_set}_tre_loss_{count}":loss,f"{which_set}_neg_energy_{count}":energy})
        count+=1
    

    if spec_norms.size > 0:
        logger.info("spec norms: {}".format(spec_norms))

    return tre_loss


# noinspection PyUnresolvedReferences
def eval_data_dep_metrics(g, sess, dp, config):
    """Compute learning metrics that are dependent on the input data."""

    n_av_metrics = len(g.average_metric_ops)
    average_metrics = [[] for _ in range(n_av_metrics)]

    dp._curr_batch = 0
    for batch in dp:

        feed_dict = get_feed_dict(g, sess, dp, batch, config, lr=-1, j=-1, train=False)
        av_res = sess.run(g.average_metric_ops, feed_dict=feed_dict)

        for i in range(n_av_metrics):
            average_metrics[i].append(av_res[i])

    average_metrics = [np.mean(np.array(m), axis=0) for m in average_metrics]  # n_metrics arrays

    dp._curr_batch = 0

    return average_metrics


def plot_waymark_diagnostic_figs(sess, g, waymark_idxs, bridge_idxs, feed_dict, dp, config):
    """Misc visualisations to inspect waymarks"""

    max_n_states = len(waymark_idxs)
    wmark_batch = sess.run(g.wmark_data, feed_dict=feed_dict)
    if config.do_mutual_info_estimation:
        x, y = wmark_batch
        wmark_batch = np.concatenate([np.expand_dims(x, 1), y], axis=1)  # (n_batch, n_waymarks+1, *event_dims)
        max_n_states += 1

    plot_chains_main(wmark_batch, "waymark_samples", config.save_dir + "figs/", dp, config=config, max_n_states=max_n_states)

    if "2d" in config.dataset_name or "1d" in config.dataset_name:
        plot_1d_or_2d_waymark_diagnostic_figs(bridge_idxs, config, dp, g, sess, waymark_idxs, wmark_batch)


def plot_1d_or_2d_waymark_diagnostic_figs(bridge_idxs, config, dp, g, sess, waymark_idxs, wmark_batch):
    n_wmarks = wmark_batch.shape[1]
    for i in range(n_wmarks):
        plot_hists_for_each_dim(config.n_dims,
                                wmark_batch[:, i, ...].reshape(-1, config.n_dims),
                                dir_name=config.save_dir + "figs/",
                                filename="waymark_{}_hists".format(i),
                                include_scatter=True,
                                alpha=1.0)
    plot_hists_for_each_dim(config.n_dims,
                            [wmark_batch[:, i, ...].reshape(-1, config.n_dims) for i in range(n_wmarks)],
                            dir_name=config.save_dir + "figs/",
                            filename="waymark_overlaid_hists",
                            include_scatter=True,
                            alpha=1.0)
    wmark_data = tf_batched_operation(sess,
                                      g.wmark_data,
                                      n_samples=len(dp.data),
                                      batch_size=config.n_batch,
                                      data_pholder=g.data,
                                      data=dp.data,
                                      const_feed_dict={g.waymark_idxs: waymark_idxs,
                                                       g.bridge_idxs: bridge_idxs}
                                      )
    dp.source_1d_or_2d.plot_sequences(wmark_data, dir_name=config.save_dir + "figs/",
                                      name="waymark_data", s=0.05, label_type="real_waymarks")


# noinspection PyUnresolvedReferences
def save_best_results(config):
    save_config(config)
    metrics_save_dir = get_metrics_data_dir(config.save_dir, epoch_i=config.epoch_idx)
    best_save_dir = get_metrics_data_dir(config.save_dir, epoch_i="best")
    copytree(metrics_save_dir, best_save_dir)


# noinspection PyUnresolvedReferences
def save_lowdim_energies(g, sess, config, dp):

    fig_data_dir = config.save_dir + "figs/data/"
    os.makedirs(fig_data_dir, exist_ok=True)

    if config.dataset_name == "1d_gauss" and \
            config.data_args["n_gaussians"] == 1 and \
            config.noise_dist_name == "gaussian" and \
            not config.data_args.get("outliers", False):

        waymark_idxs, bridge_idxs = get_waymark_and_bridge_idxs_for_epoch_i(config, -1)
        noise_coefs = sess.run(g.waymark_construction_results.noise_multipliers,
                               feed_dict={g.waymark_idxs: waymark_idxs, g.bridge_idxs: bridge_idxs})
        data_coefs = (1 - noise_coefs**2)**0.5

        means = data_coefs * config.data_args["mean"] + noise_coefs * config.noise_dist_gaussian_loc
        vars = ((data_coefs**2) * config.data_args["std"]**2) + ((noise_coefs**2) * config.noise_dist_gaussian_std**2)
        true_wmarks = [norm(loc=m, scale=s) for m, s in zip(means, vars**0.5)]

        plot_and_save_1dgauss_logratio_metrics(config, dp, g, sess, true_wmarks)
    else:
        true_wmarks = None

    # for gridsize in ["small", "medium", "large"]:
    for gridsize in ["large"]:

        tst_grid_coords = getattr(dp.source_1d_or_2d, "tst_coords_{}".format(gridsize))

        feed_dict = get_feed_dict(g, sess, dp, tst_grid_coords, config, train=False)
        logr_vals_at_p = sess.run(g.bridges_and_noise_neg_e_of_data, feed_dict)  # (n_tst, n_ratios+1)

        dp.source_1d_or_2d.plot_logratios(logr_vals_at_p, config.save_dir + "figs/", "{}_density_plots".format(gridsize),
                                          gridsize=gridsize, true_wmarks=true_wmarks)
        dp.source_1d_or_2d.plot_logratios(logr_vals_at_p, config.save_dir + "figs/", "{}_log_density_plots".format(gridsize),
                                          log_domain=True, gridsize=gridsize, true_wmarks=true_wmarks)

        np.savez(os.path.join(fig_data_dir, "ratios_on_{}_tst_grid".format(gridsize)),
                 xaxis=tst_grid_coords, logratio_vals=logr_vals_at_p)
        np.savez(os.path.join(fig_data_dir, "model_on_{}_tst_grid".format(gridsize)),
                 xaxis=tst_grid_coords, logp_model=logr_vals_at_p.sum(-1))


def plot_and_save_1dgauss_logratio_metrics(config, dp, g, sess, true_wmarks):

    name = "1d_gaussian_" + \
           f"mu1_{config.data_args['mean']}_" + \
           f"sigma1_{config.data_args['std']}_" + \
           f"mu2_{config.noise_dist_gaussian_loc}_" + \
           f"sigma2_{config.noise_dist_gaussian_std}"

    save_dir = project_root + "notebooks/" + name
    os.makedirs(save_dir, exist_ok=True)
    print("Saving 1d log-ratio figures to ", save_dir)

    # Sample from p, q and u (uniform distribution on [-20, 20])
    num_samples = 1000
    p_batch = true_wmarks[0].rvs(num_samples).reshape(-1, 1)
    q_batch = true_wmarks[-1].rvs(num_samples).reshape(-1, 1)
    u_batch = np.random.uniform(-20, 20, num_samples).reshape(-1, 1)

    feed_dict = get_feed_dict(g, sess, dp, p_batch, config, train=False)
    logr_vals_at_p = sess.run(g.bridges_and_noise_neg_e_of_data, feed_dict)

    feed_dict = get_feed_dict(g, sess, dp, q_batch, config, train=False)
    logr_vals_at_q = sess.run(g.bridges_and_noise_neg_e_of_data, feed_dict)

    feed_dict = get_feed_dict(g, sess, dp, u_batch, config, train=False)
    logr_vals_at_u = sess.run(g.bridges_and_noise_neg_e_of_data, feed_dict)

    # estimated logratio under samples from p, q & u
    estimated_logratio_vals_at_p = logr_vals_at_p[:, :-1].sum(-1)  # (1000,)
    estimated_logratio_vals_at_q = logr_vals_at_q[:, :-1].sum(-1)  # (1000,)
    estimated_logratio_vals_at_u = logr_vals_at_u[:, :-1].sum(-1)  # (1000,)

    # estimated KL
    kl_estimate = estimated_logratio_vals_at_p.mean()

    # true logratio under samples from p, q & u
    true_logratio_vals_at_p = np.squeeze(true_wmarks[0].logpdf(p_batch)) - np.squeeze(
        true_wmarks[-1].logpdf(p_batch))
    true_logratio_vals_at_q = np.squeeze(true_wmarks[0].logpdf(q_batch)) - np.squeeze(
        true_wmarks[-1].logpdf(q_batch))
    true_logratio_vals_at_u = np.squeeze(true_wmarks[0].logpdf(u_batch)) - np.squeeze(
        true_wmarks[-1].logpdf(u_batch))

    # true KL
    true_kl = true_logratio_vals_at_p.mean()

    # plot logr(x) where x ~ uniform
    _plot_1d_logratio(u_batch, true_logratio_vals_at_u, estimated_logratio_vals_at_u, save_dir, "uniform")

    _plot_1d_logratio(p_batch, true_logratio_vals_at_p, estimated_logratio_vals_at_p, save_dir, "p")
    _plot_1d_logratio(q_batch, true_logratio_vals_at_q, estimated_logratio_vals_at_q, save_dir, "q")

    # plot logr(x) where x ~ 0.5p + 0.5q
    _plot_1d_logratio(
        np.concatenate([p_batch, q_batch], axis=0),
        np.concatenate([true_logratio_vals_at_p, true_logratio_vals_at_q], axis=0),
        np.concatenate([estimated_logratio_vals_at_p, estimated_logratio_vals_at_q], axis=0),
        save_dir,
        "pq_mixture"
    )

    # Scatter plot of true logratio (x-axis) versus estimated logratio (y-axis)
    fig, ax = plt.subplots(1, 1)
    ax.scatter(true_logratio_vals_at_p, estimated_logratio_vals_at_p, label="samples from p")
    ax.scatter(true_logratio_vals_at_q, estimated_logratio_vals_at_q, label="samples from q")
    ax.scatter(true_logratio_vals_at_u, estimated_logratio_vals_at_u, label="samples from u")
    ax.set_xlabel("True logratio")
    ax.set_ylabel("Estimated logratio")

    # add diagonal line to scatter plot
    min_v = min(true_logratio_vals_at_q.min(), estimated_logratio_vals_at_q.min(),
                true_logratio_vals_at_p.min(), estimated_logratio_vals_at_p.min())
    max_v = max(true_logratio_vals_at_q.max(), estimated_logratio_vals_at_q.max(),
                true_logratio_vals_at_p.max(), estimated_logratio_vals_at_p.max())
    line = np.linspace(min_v, max_v, 128)
    ax.plot(line, line, linestyle="-")
    ax.legend()

    save_fig(save_dir, "true_logratios_vs_estimated")

    # save plotting data to disk
    np.savez(os.path.join(save_dir, f"data"),
             p_samples=p_batch,
             q_samples=q_batch,
             u_samples=u_batch,
             estimated_logratio_vals_at_p=estimated_logratio_vals_at_p,
             estimated_logratio_vals_at_q=estimated_logratio_vals_at_q,
             estimated_logratio_vals_at_u=estimated_logratio_vals_at_u,
             true_logratio_vals_at_p=true_logratio_vals_at_p,
             true_logratio_vals_at_q=true_logratio_vals_at_q,
             true_logratio_vals_at_u=true_logratio_vals_at_u,
             estimated_kl=kl_estimate,
             true_kl=true_kl
             )


def _plot_1d_logratio(u_batch, true_logratio_vals_at_u,
                      estimated_logratio_vals_at_u, save_dir, dist="uniform"):
    """plot estimated & true logr(x)"""
    fig, ax = plt.subplots(1, 1)
    ax.scatter(u_batch, true_logratio_vals_at_u, label="True")
    ax.scatter(u_batch, estimated_logratio_vals_at_u, label="Estimated")
    ax.set_xlabel("x")
    ax.set_ylabel(r"$\log r(x)$")
    fig.legend()
    save_fig(save_dir, f"logratios_under_{dist}")


# noinspection PyUnresolvedReferences
def make_savers(config):
    energy_vars = tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.GLOBAL_VARIABLES, scope='tre_model/')
    if config.save_every_x_epochs:
        num_to_save = int(config.n_epochs / config.save_every_x_epochs)
        saver1 = tf.compat.v1.train.Saver(var_list=energy_vars, max_to_keep=num_to_save, save_relative_paths=True)
    else:
        saver1 = None
    saver2 = tf.compat.v1.train.Saver(var_list=energy_vars, max_to_keep=2, save_relative_paths=True)
    return saver1, saver2


def set_debug_params(args, config):
    if args.debug != -1:
        config["n_epochs"] = 2
        config["frac"] = 0.05
        config["mlp_hidden_size"] = 64
        config["mlp_n_blocks"] = 1
        config["channel_widths"] = [[1]]
        # config["channel_widths"] = [[10], [10, 10]]

        if config["noise_dist_name"] == "flow":
            if config["flow_type"] == "GLOW":
                config["glow_depth"] = 2
                if config["dataset_name"] == "mnist":
                    config["flow_id"] = "20200406-1408_0"
            if config["flow_type"] == "GaussianCopula":
                if config["dataset_name"] == "mnist":
                    config["flow_id"] = "20200504-1022_0"


def load_config():
    """load & augment experiment configuration"""
    parser = ArgumentParser(description='Train TRE model.', formatter_class=ArgumentDefaultsHelpFormatter)
    parser.add_argument('--config_path', type=str, default="1d_gauss/model/0")
    # parser.add_argument('--config_path', type=str, default="gaussians/model/0")
    # parser.add_argument('--config_path', type=str, default="mnist/model/0")
    # parser.add_argument('--config_path', type=str, default="multiomniglot/model/0")
    parser.add_argument('--restore_model', type=int, default=-1)
    parser.add_argument('--only_eval_model', type=int, default=-1)
    parser.add_argument('--analyse_1d_objective', type=int, default=-1)
    parser.add_argument('--analyse_single_sample_size', type=int, default=0)
    parser.add_argument('--load_1d_arrays_from_disk', type=int, default=-1)
    parser.add_argument('--debug', type=int, default=-1)
    args = parser.parse_args()

    args.restore_model = False if args.restore_model == -1 else True

    root = "saved_models" if args.restore_model else "configs"
    with open(project_root + "{}/{}.json".format(root, args.config_path)) as f:
        config = json.load(f)
        

    if not args.restore_model:
        config = merge_dicts(*list(config.values()))  # json is 2-layers deep, flatten it

    rename_save_dir(config)
    config.update(vars(args))

    config["config_id"] = args.config_path.split("/")[-1]
    config["only_eval_model"] = False if args.only_eval_model == -1 else True
    config["analyse_1d_objective"] = False if args.analyse_1d_objective == -1 else True
    config["analyse_single_sample_size"] = False if args.analyse_single_sample_size == -1 else True
    config["load_1d_arrays_from_disk"] = False if args.load_1d_arrays_from_disk == -1 else True
    config["debug"] = False if args.debug == -1 else True

    set_debug_params(args, config)
    save_config(config)

    return AttrDict(config)


# noinspection PyUnresolvedReferences,PyTypeChecker
def main():
    """Run density estimation experiment with telescoping density-ratio estimation"""
    make_logger()
    logger = logging.getLogger("tf")
    #np.set_printoptions(precision=2)
    
    # load a config which is created after running make_configs.py
    config = load_config()
    wandb.init(project="tre", config=config,name=config.run_name)

    
    # load data provider objects that can be iterated through to obtain batches of data
    train_dp, val_dp = load_data_providers_and_update_conf(config)

    # create a dictionary whose keys are tensorflow operations that can be accessed like attributes e.g graph.operation
    graph = build_graph(config)

    with tf.compat.v1.Session() as sess:
        sess.run(tf.compat.v1.global_variables_initializer())

        if config.restore_model:
            load_model(sess, "best", config)

        if config.noise_dist_name == "flow":
            logger.info("Loading copula/flow noise distribution...")
            load_flow(sess, config, config.flow_id)
            logger.info("Loaded!")

        saver1, saver2 = make_savers(config)

        # if using the 1d gaussian dataset, analyse the objective function
        if config.analyse_1d_objective:
            if config.analyse_single_sample_size:
                analyse_objective_fn_for_1d_gauss(graph, sess, train_dp, config, get_feed_dict, do_plot=True,
                                                  do_load=config.load_1d_arrays_from_disk)
            else:
                analyse_objective_for_1d_gauss_multiple_sample_sizes(config, graph, sess, train_dp, get_feed_dict)
            return

        # either eval a pre-trained model, or train a new model
        if config.only_eval_model:
            post_learning_summary(graph, sess, train_dp, val_dp, config)
        else:
            train(graph, sess, train_dp, val_dp, saver1, saver2, config)

        logger.info("-------------------------------------------------")
        logger.info("             Completed training                  ")
        logger.info("-------------------------------------------------")

    save_config(config)
    os.makedirs(config.save_dir, exist_ok=True)
    with open(os.path.join(config.save_dir, "finished.txt"), 'w+') as f:
        f.write("finished.")


if __name__ == "__main__":
    main()
