import time
import torch

from torch.cuda.amp import autocast
from monai.data import decollate_batch
from monai.metrics import LossMetric, Cumulative


def train_epoch(model, loader, optimizer, criterion, device, scaler, amp=True):
    model.train()
    start_time = time.time()
    run_loss = LossMetric(loss_fn=criterion)
    for idx, batch in enumerate(loader):
        data, target = batch["image"], batch["label"]
        data, target = data.to(device), target.to(device)
        modality = None
        if "modality" in batch.keys():
            modality = batch["modality"]
            modality = modality.to(device)
        optimizer.zero_grad()
        with autocast(enabled=amp):
            output = model(data, modality)
            loss = criterion(output, target)
            run_loss(output, target)
        print(f"Train batch {idx}, loss {loss.item()}")
        # If AMP is active
        if amp:
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            optimizer.step()
        # Update distributed running loss
        # run_loss.update(loss)
        break
    # Total loss
    epoch_loss = run_loss.aggregate(reduction='mean').item()
    # Important to reset and free memory
    run_loss.reset()
    return epoch_loss


def val_epoch(
        model,
        loader,
        criterion,
        device,
        acc_func,
        post_label,
        post_pred,
        model_inferer=None,
        amp=True,
        surface_distance=None,
        additional_metrics=None,
        logger=None,
        epoch=None):
    model.eval()
    start_time = time.time()
    run_loss = LossMetric(loss_fn=criterion)
    # Here we will store accuracy per modality
    acc_mod_cumulative = Cumulative()
    if surface_distance is not None:
        surface_mod_cumulative = Cumulative()
    with torch.no_grad():
        for idx, batch in enumerate(loader):
            data, target = batch["image"], batch["label"]
            data, target = data.to(device), target.to(device)
            modality = None
            if "modality" in batch.keys():
                modality = batch["modality"]
                modality = modality.to(device)
            with autocast(enabled=amp):
                if model_inferer is not None:
                    output = model_inferer(data, modalities=modality)
                else:
                    output = model(data, modality)
            loss = criterion(output, target)
            run_loss(output, target)
            # In the print we assume validation batch
            print(f"Val batch {idx} modality {modality.tolist()}, loss {loss}")
            # run_loss.update(loss)
            # Compute accuracy
            # acc_func.update(output, target.int())
            val_labels_list = decollate_batch(target)
            val_labels_convert = [post_label(val_label_tensor) for val_label_tensor in val_labels_list]
            val_outputs_list = decollate_batch(output)
            val_output_convert = [post_pred(val_pred_tensor) for val_pred_tensor in val_outputs_list]
            # Back to [BCHWD] for metrics computation
            val_output_convert = torch.stack(val_output_convert)
            val_labels_convert = torch.stack(val_labels_convert)
            batch_acc = acc_func(y_pred=val_output_convert, y=val_labels_convert)
            print(f"Accuracy batch {idx} modality {modality.tolist()}, loss {batch_acc}")
            # acc_mod_cumulative.append(batch_acc, modality)
            acc_mod_cumulative.extend(batch_acc, modality)  # Extend is for append a batch-first array
            # Update surface distance
            if surface_distance is not None:
                batch_surface = surface_distance(y_pred=val_output_convert, y=val_labels_convert)
                surface_mod_cumulative.extend(batch_surface, modality)
                print(f"Surface batch {idx} modality {modality.tolist()}, loss {batch_surface}")
            # Update additional metrics is any
            if additional_metrics:
                for metric in additional_metrics:
                    # .cpu() is a workaround for monai issue
                    metric(y_pred=val_output_convert.cpu(), y=val_labels_convert.cpu())

    # Here I will have a Tensor of size [N_batches, N_sample_per_batch, N_classes]
    acc, mod = acc_mod_cumulative.get_buffer()
    # This is a workaround for the cuda problem of monai metrics
    surf = acc.cpu()
    mod_surf = mod.cpu()
    # Flatten on first dim to have [N_samples, N_classes] -> Not needed with extend instead of append
    # acc = acc.flatten(end_dim=1)
    # mod = mod.flatten(end_dim=1)
    if logger is not None:
        print(acc, mod)
    for m in torch.unique(mod):
        # Select only samples of that modality
        acc_m = acc[mod == m]
        # Reduce per modality (see monai.metrics.utils.py)
        nans = torch.isnan(acc_m)
        not_nans = (~nans).float()
        t_zero = torch.zeros(1, device=acc_m.device, dtype=acc_m.dtype)
        not_nans = not_nans.sum(dim=0)
        acc_m[nans] = 0
        # We have the accuracy per class here
        acc_m = torch.where(not_nans > 0, acc_m.sum(dim=0) / not_nans, t_zero)  # batch average
        if logger is not None:
            print(f"Accuracy per class [modality {m}]: {acc_m.tolist()}")
            dict_acc_class_modality = {}
            for c, v in enumerate(acc_m.tolist()):
                dict_acc_class_modality[f"val_modality{m}_dice/class{c}"] = v
            logger.log(dict_acc_class_modality, epoch)
            # Average accuracy. Note: as in monai we don't account for classes with all nans in the average
            # This can make the average accuracy among modalities different from the total average accuracy !!
            print(f"Average Accuracy [modality {m}]: {torch.nanmean(acc_m[not_nans > 0]).item()}")
            logger.log({f"val_modality{m}_dice/avg": torch.nanmean(acc_m[not_nans > 0]).item()}, epoch)
    if surface_distance is not None:
        surf, mod_surf = surface_mod_cumulative.get_buffer()
        # This is a workaround for the cuda problem of monai metrics
        surf = surf.cpu()
        mod_surf = mod_surf.cpu()
        if logger is not None:
            print(surf, mod_surf)
        for m in torch.unique(mod_surf):
            # Select only samples of that modality
            surf_m = surf[mod_surf == m]
            # Reduce per modality (see monai.metrics.utils.py)
            nans = torch.isnan(surf_m)
            not_nans = (~nans).float()
            t_zero = torch.zeros(1, device=surf_m.device, dtype=surf_m.dtype)
            not_nans = not_nans.sum(dim=0)
            surf_m[nans] = 0
            # We have the surface distance per class here
            surf_m = torch.where(not_nans > 0, surf_m.sum(dim=0) / not_nans, t_zero)  # batch average
            if logger is not None:
                print(f"Surface distance per class [modality {m}]: {surf_m.tolist()}")
                dict_surf_class_modality = {}
                for c, v in enumerate(surf_m.tolist()):
                    dict_surf_class_modality[f"val_modality{m}_surface_distance/class{c}"] = v
                logger.log(dict_surf_class_modality, epoch)
                # Average surface distance. Note: as in monai we don't account for classes with all nans in the average
                # This can make the average surface distance among modalities different from the total average accuracy
                print(f"Average Surface Distance [modality {m}]: {torch.nanmean(surf_m[not_nans > 0]).item()}")
                logger.log({f"val_modality{m}_surface_distance/avg": torch.nanmean(surf_m[not_nans > 0]).item()}, epoch)
    epoch_loss = run_loss.aggregate(reduction='mean').item()
    accuracy, not_nans = acc_func.aggregate()
    if logger is not None:
        print(f"Accuracy per class [tot]: {accuracy.tolist()}")
        dict_acc_class = {}
        for c, v in enumerate(accuracy.tolist()):
            dict_acc_class[f"val_total_dice/class{c}"] = v
        logger.log(dict_acc_class, epoch)
    if surface_distance is not None:
        surface, not_nans_surface = surface_distance.aggregate()
        if logger is not None:
            print(f"Surface per class [tot]: {surface.tolist()}")
            dict_surf_class = {}
            for c, v in enumerate(accuracy.tolist()):
                dict_surf_class[f"val_total_surface_distance/class{c}"] = v
            logger.log(dict_surf_class, epoch)
        surface_distance.reset()
        surface_mod_cumulative.reset()
    # Important to reset and free memory
    run_loss.reset()
    acc_func.reset()
    acc_mod_cumulative.reset()
    # Aggregate additional metric, if any
    metrics = []
    if additional_metrics:
        for metric in additional_metrics:
            metrics.append(metric.aggregate().item())
            metric.reset()
    if surface_distance is not None:
        return epoch_loss, torch.nanmean(accuracy[not_nans > 0]).item(), \
               torch.nanmean(surface[not_nans_surface > 0]).item(), metrics
    else:
        return epoch_loss, torch.nanmean(accuracy[not_nans > 0]).item(), metrics