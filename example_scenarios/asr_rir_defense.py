"""
Automatic speech recognition scenario
"""

import logging
from typing import Optional

from tqdm import tqdm
import numpy as np
from art.preprocessing.audio import LFilter, LFilterPyTorch
import scipy.io.wavfile as wav

from armory.utils.config_loading import (
    load_dataset,
    load_model,
    load_attack,
    load_adversarial_dataset,
    load_defense_wrapper,
    load_defense_internal,
    load_label_targeter,
)
from armory.utils import metrics
from armory.scenarios.base import Scenario
from armory.utils.export import SampleExporter

import torch
import numpy.fft as fft

logger = logging.getLogger(__name__)


def Readrir(num_room):
    """
    Return:
        rir: a numpy array of the room reverberation
        (make sure the test rooms are different from training rooms)

    """
    index = num_room + 1000 + 1
    _, rir = wav.read(
        "./rir/LibriSpeech/test-clean/3575/170457/3575-170457-0013" + "_rir_" + str(index) + ".wav")
    return rir


def create_speech_rir(audios, rir):
    """
    Returns:
        A tensor of speech with reverberations (Convolve the audio with the rir)
    """
    batch_size = audios.shape[0]
    speech_rir = []

    for i in range(batch_size):
        s1 = len(audios[i])
        s2 = len(rir)
        shape = s1 + s2 - 1

        # Compute convolution in fourier space
        sp1 = fft.rfft(rir, n=shape)
        sp2 = fft.rfft(audios[i], n=shape)

        ret = fft.irfft(sp1 * sp2, n=shape)

        # normalization
        ret /= np.amax(np.absolute(ret))
        ret *= 2 ** (16 - 1) - 1
        ret = np.clip(ret, a_min=-(2 ** (16 - 1)), a_max=2 ** (16 - 1) - 1)
        ret = np.pad(ret, (0, 100000), 'constant', constant_values=(0, 0))
        ret = ret[:s1]

        speech_rir.append(ret)
    speech_rirs = np.array(speech_rir)
    return speech_rirs


def load_audio_channel(delay, attenuation, pytorch=True):
    """
    Return an art LFilter object for a simple delay (multipath) channel

    If attenuation == 0 or delay == 0, return an identity channel
        Otherwise, return a channel with length equal to delay + 1

    NOTE: lfilter truncates the end of the echo, so output length equals input length
    """
    delay = int(delay)
    attenuation = float(attenuation)
    if delay < 0:
        raise ValueError(
            f"delay {delay} must be a nonnegative number (of samples)")
    if delay == 0 or attenuation == 0:
        logger.warning("Using an identity channel")
        numerator_coef = np.array([1.0])
        denominator_coef = np.array([1.0])
    else:
        if not (-1 <= attenuation <= 1):
            logger.warning(f"filter attenuation {attenuation} not in [-1, 1]")

        # Simple FIR filter with a single multipath delay
        numerator_coef = np.zeros(delay + 1)
        numerator_coef[0] = 1.0
        numerator_coef[delay] = attenuation

        denominator_coef = np.zeros_like(numerator_coef)
        denominator_coef[0] = 1.0

    if pytorch:
        try:
            return LFilterPyTorch(
                numerator_coef=numerator_coef, denominator_coef=denominator_coef
            )
        except ImportError:
            logger.exception(
                "PyTorch not available. Resorting to scipy filter")

    logger.warning(
        "Scipy LFilter does not currently implement proper gradients")
    return LFilter(numerator_coef=numerator_coef, denominator_coef=denominator_coef)


def to_wav(signal, fs, filename, norm=False, bitdepth=np.float):
    """
    Save the signal to a wav files.
    """
    from scipy.io import wavfile
    # if mono is True:
    #     signal = self.signals[self.M // 2]
    # else:
    #     signal = self.signals.T  # each column is a channel
    float_types = [float, np.float, np.float32, np.float64]
    if bitdepth in float_types:
        bits = None
    elif bitdepth is np.int8:
        bits = 8
    elif bitdepth is np.int16:
        bits = 16
    elif bitdepth is np.int32:
        bits = 32
    elif bitdepth is np.int64:
        bits = 64
    else:
        raise NameError("No such type.")

    if norm:
        signal = normalize(signal, bits=bits)
    signal = np.array(signal, dtype=bitdepth)
    breakpoint()
    wavfile.write(filename, fs, signal)


class AutomaticSpeechRecognition(Scenario):
    def _evaluate(
        self,
        config: dict,
        num_eval_batches: Optional[int],
        skip_benign: Optional[bool],
        skip_attack: Optional[bool],
        skip_misclassified: Optional[bool],
    ) -> dict:
        """
        Evaluate the config and return a results dict
        """
        if skip_misclassified:
            raise ValueError(
                "skip_misclassified shouldn't be set for ASR scenario")
        model_config = config["model"]
        estimator, fit_preprocessing_fn = load_model(model_config)

        audio_channel_config = config.get("adhoc", {}).get("audio_channel")
        if audio_channel_config is not None:
            logger.info("loading audio channel")
            for k in "delay", "attenuation":
                if k not in audio_channel_config:
                    raise ValueError(f"audio_channel must have key {k}")
            audio_channel = load_audio_channel(**audio_channel_config)
            if estimator.preprocessing_defences:
                estimator.preprocessing_defences.insert(0, audio_channel)
            else:
                estimator.preprocessing_defences = [audio_channel]
            estimator._update_preprocessing_operations()

        defense_config = config.get("defense") or {}
        defense_type = defense_config.get("type")

        if defense_type in ["Preprocessor", "Postprocessor"]:
            logger.info(
                f"Applying internal {defense_type} defense to estimator")
            estimator = load_defense_internal(config["defense"], estimator)

        if model_config["fit"]:
            logger.info(
                f"Fitting model {model_config['module']}.{model_config['name']}..."
            )
            fit_kwargs = model_config["fit_kwargs"]

            logger.info(
                f"Loading train dataset {config['dataset']['name']}...")
            batch_size = config["dataset"].pop("batch_size")
            config["dataset"]["batch_size"] = fit_kwargs.get(
                "fit_batch_size", batch_size
            )
            train_data = load_dataset(
                config["dataset"],
                epochs=fit_kwargs["nb_epochs"],
                split=config["dataset"].get("train_split", "train_clean100"),
                preprocessing_fn=fit_preprocessing_fn,
                shuffle_files=True,
            )
            config["dataset"]["batch_size"] = batch_size
            if defense_type == "Trainer":
                logger.info(f"Training with {defense_type} defense...")
                defense = load_defense_wrapper(config["defense"], estimator)
                defense.fit_generator(train_data, **fit_kwargs)
            else:
                logger.info("Fitting estimator on clean train dataset...")
                estimator.fit_generator(train_data, **fit_kwargs)

        if defense_type == "Transform":
            # NOTE: Transform currently not supported
            logger.info(
                f"Transforming estimator with {defense_type} defense...")
            defense = load_defense_wrapper(config["defense"], estimator)
            estimator = defense()

        attack_config = config["attack"]
        attack_type = attack_config.get("type")

        targeted = bool(attack_config.get("targeted"))
        metrics_logger = metrics.MetricsLogger.from_config(
            config["metric"],
            skip_benign=skip_benign,
            skip_attack=skip_attack,
            targeted=targeted,
        )

        if config["dataset"]["batch_size"] != 1:
            logger.warning("Evaluation batch_size != 1 may not be supported.")

        predict_kwargs = config["model"].get("predict_kwargs", {})
        eval_split = config["dataset"].get("eval_split", "test_clean")
        rir = Readrir(0)

        if skip_benign:
            logger.info("Skipping benign classification...")
        else:
            # Evaluate the ART estimator on benign test examples
            logger.info(f"Loading test dataset {config['dataset']['name']}...")
            test_data = load_dataset(
                config["dataset"],
                epochs=1,
                split=eval_split,
                num_batches=num_eval_batches,
                shuffle_files=False,
            )
            logger.info("Running inference on benign examples...")
            for x, y in tqdm(test_data, desc="Benign"):
                # Ensure that input sample isn't overwritten by estimator
                # Apply RIR
                x = create_speech_rir(x, rir)
                to_wav(x[0], 16000, "/workspace/test.wav")
                breakpoint()

                x.flags.writeable = False
                with metrics.resource_context(
                    name="Inference",
                    profiler=config["metric"].get("profiler_type"),
                    computational_resource_dict=metrics_logger.computational_resource_dict,
                ):
                    y_pred = estimator.predict(x, **predict_kwargs)
                metrics_logger.update_task(y, y_pred)
            metrics_logger.log_task()

        if skip_attack:
            logger.info("Skipping attack generation...")
            return metrics_logger.results()

        # Imperceptible attack still WIP
        if (config.get("adhoc") or {}).get("skip_adversarial"):
            logger.info("Skipping adversarial classification...")
            return metrics_logger.results()

        # Evaluate the ART estimator on adversarial test examples
        logger.info("Generating or loading / testing adversarial examples...")

        if attack_type == "preloaded":
            test_data = load_adversarial_dataset(
                attack_config,
                epochs=1,
                split="adversarial",
                num_batches=num_eval_batches,
                shuffle_files=False,
            )
        else:
            attack = load_attack(attack_config, estimator)
            if targeted != attack.targeted:
                logger.warning(
                    f"targeted config {targeted} != attack field {attack.targeted}"
                )
            test_data = load_dataset(
                config["dataset"],
                epochs=1,
                split=eval_split,
                num_batches=num_eval_batches,
                shuffle_files=False,
            )
            if targeted:
                label_targeter = load_label_targeter(
                    attack_config["targeted_labels"])

        export_samples = config["scenario"].get("export_samples")
        if export_samples is not None and export_samples > 0:
            sample_exporter = SampleExporter(
                self.scenario_output_dir, test_data.context, export_samples
            )
        else:
            sample_exporter = None

        for x, y in tqdm(test_data, desc="Attack"):
            with metrics.resource_context(
                name="Attack",
                profiler=config["metric"].get("profiler_type"),
                computational_resource_dict=metrics_logger.computational_resource_dict,
            ):
                if attack_type == "preloaded":
                    x, x_adv = x
                    if targeted:
                        y, y_target = y
                elif attack_config.get("use_label"):
                    x_adv = attack.generate(x=x, y=y)
                elif targeted:
                    y_target = label_targeter.generate(y)
                    x_adv = attack.generate(x=x, y=y_target)
                else:
                    x_adv = attack.generate(x=x)

            # Apply RIR
            x_adv = create_speech_rir(x_adv, rir)
            to_wav(x_adv, 16000, "~/test.wav")
            # break

            # Ensure that input sample isn't overwritten by estimator
            x_adv.flags.writeable = False
            y_pred_adv = estimator.predict(x_adv, **predict_kwargs)
            metrics_logger.update_task(y, y_pred_adv, adversarial=True)
            if targeted:
                metrics_logger.update_task(
                    y_target, y_pred_adv, adversarial=True, targeted=True,
                )
            metrics_logger.update_perturbation(x, x_adv)
            if sample_exporter is not None:
                sample_exporter.export(x, x_adv, y, y_pred_adv)
        metrics_logger.log_task(adversarial=True)
        if targeted:
            metrics_logger.log_task(adversarial=True, targeted=True)
        return metrics_logger.results()
