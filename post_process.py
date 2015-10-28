# Cleans up trajectory files and joins them into one file.

import argparse
import cv2
import datetime as dt
import gc
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.colors import LogNorm
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
assert Axes3D
from multi_tracker import get_log_kernel, process_frame, get_roi_mask, \
    get_thresh_kernel_size
import numpy as np
from numpy.linalg import norm
import os
import pandas as pd
import sys


def get_metadata(filename):
    '''
    Args:
        filename - path of trajectory file
    Returns:
        camera_name, date_time, scaling_factor
    '''
    basename = os.path.basename(filename)
    i = basename.find('-')
    camera_name = basename[:i]
    date_time = dt.datetime.strptime(basename[i + 1:i + 20],
                                     '%Y-%m-%d-%H-%M-%S')
    scaling_factor = float(basename.split('-')[-2])

    return camera_name, date_time, scaling_factor


def get_filenames(trajdir, cond_file, time_offset=9):
    '''
    Produce a dataframe containing the paths of all trajectory files, indexed
    by condition then date.
    Args:
        trajdir - directory to look for raw trajectory files
        cond_file - path of csv file containing the condition for each date
    Returns:
        DataFrame indexed by 'condition' and 'date' with column 'path'
    '''
    cond_df = pd.read_csv(cond_file, parse_dates=['Date'], index_col='Date',
                          dayfirst=True)
    offset_delta = dt.timedelta(hours=time_offset)
    file_list = []
    for f in os.listdir(trajdir):
        if f[-8:] == 'traj.csv':
            metadata = get_metadata(f)
            date = (metadata[1] - offset_delta).date()
            condition = cond_df.loc[date, metadata[0]]
            file_list.append([condition, date, '/'.join([trajdir, f])])

    files_df = pd.DataFrame(np.array(file_list), columns=['condition', 'date',
                                                          'path'])

    return files_df.sort_values(by=['condition', 'date', 'path']).set_index(
        ['condition', 'date'])


def parse_traj_file(path, n):
    '''
    Parses a trajectory file.
    Args:
        path - path of trajectory file
        n - number of tracks in trajectory file
    Returns:
        dataframe indexed by time and traj
    '''
    df_list = [pd.read_csv(
        path, header=None, names=['t', 'traj', 'x', 'y'],
        usecols=[0, i * 3 + 1, i * 3 + 2, i * 3 + 3]) for i in range(n)]
    df = pd.concat(df_list)
    df.sort_values(by=['traj', 't'], inplace=True)

    return df.reindex_axis(['traj', 't', 'x', 'y'], axis=1)


def combine_traj_files(files, n):
    '''
    Parses and combines trajectory files with corrected times and traj indices.
    Args:
        files - filepaths to combine
        n - number of tracks in each file
    Returns:
        a complete trajectory dataframe, indexed by 'traj'
        (will be large - up to 200mb)
    '''
    df_list = []
    first = True
    traj_max = -1
    i = 0
    for path in files:
        i += 1
        sys.stdout.write('\rParsing file %s/%s' % (i, len(files)))
        filename = os.path.basename(path)
        dtime = get_metadata(filename)[1]
        if first:
            first_time = dtime
            first = False

        df_current = parse_traj_file(path, n)
        # Update times
        df_current['t'] += (dtime - first_time).total_seconds()
        # Update trajectory indeces
        df_current['traj'] += traj_max + 1

        traj_max = int(df_current['traj'].max())
        df_list.append(df_current)

    print '\nJoining.'
    df = pd.concat(df_list)

    return df.set_index('traj')


def filter_traj(df, min_length=2, trim_start_frames=0, trim_end_frames=0):
    '''
    Removes nonsensical data from trajectory dataframe, then removes
    trajectories with length <= min_length. Finally, the last trim_frames are
    trimmed from each trajectory.
    Args:
        df - trajectory dataframe
        min_length - integer minimum length of sensical trajectory. Must be
                     larger than trim_end_frames + trim_start_frames
        trim_end_frames - integer number of frames to trim off the end of each
                          trajectory.
        trim_start_frames - integer number of frames to trim off the start of
                            each trajectory.
    Returns:
        filtered and trimmed DataFrame
    '''
    assert trim_start_frames + trim_end_frames < min_length
    print 'Removing zeroes.'
    df1 = df.loc[np.bitwise_and(df.x > 0., df.y > 0.)]

    print 'Indexing sufficiently long trajectories.'
    good_length_idxs = []
    for i in df1.index.unique():
        if len(df1.loc[i]) >= min_length:
            good_length_idxs.append(i)

    print 'Applying index.'
    df1 = df1.loc[good_length_idxs]

    print 'Trimming trajectories.'
    if trim_end_frames > 0:
        df2 = pd.concat([df1.loc[i][trim_start_frames:-trim_end_frames]
                         for i in df1.index.unique()])
        print 'Done.'
        return df2

    elif trim_start_frames > 0:
        df2 = pd.concat([df1.loc[i][trim_start_frames:]
                         for i in df1.index.unique()])
        print 'Done.'
        return df2

    else:
        print 'No trimming. Done.'
        return df1


def back_process(df):
    '''
    NOT IMPLEMENTED PROPERLY
    Infers missing start of trajectories which spawn out of another trajectory.
    This deals with the case where two (or more) bees start in the same location
    and as a result, only one bee is detected initially. This should be done
    BEFORE filter_traj is called (relies on missing data being zeros).
    Args:
        df - pandas.DataFrame with columns indexed by 't', 'x' and 'y' with
        index column 'traj'
    Returns:
        back-processed DataFrame
    '''
    # Determine indices of candidate trajectories for back processing (all
    # trajectories with start point at 0, 0).
    process_traj_idcs = []
    for i in df.index.unique():
        if df.loc[i].iloc[0].x == df.loc[i].iloc[0].y == 0.:
            process_traj_idcs.append(i)

    # Determine indices of reference tracks
    possible_pairings = []
    for i in process_traj_idcs:
        start_time = df.loc[i].loc[np.bitwise_and(
            df.loc[i].iloc[0].x != 0., df.loc[i].iloc[0].y != 0.)].t.iloc[0]
        possible_ref_idx = df[df.t == start_time].index
        xy = np.array([df.loc[i].loc[df.loc[i].iloc[0].t == start_time].x,
                       df.loc[i].loc[df.loc[i].iloc[0].t == start_time].y])

        for j in possible_ref_idx:
            rxy = np.array([df.loc[j].loc[df.loc[j].iloc[0].t == start_time].x,
                            df.loc[j].loc[df.loc[j].iloc[0].t == start_time].y])

            if norm(xy - rxy) < 5:
                possible_pairings.append([i, j])

    return possible_pairings


def heat_map(df):
    '''
    Plots heatmap of trajectories (x and y axes)
    Args:
        df - pd.DataFrame containing trajectories
    '''
    plt.hist2d(df.x, df.y, bins=100, norm=LogNorm())
    plt.colorbar()
    plt.show()


def traj_3d(df):
    '''
    Plots trajectories in 3D using matplotlib. This is pretty useless for
    anything over an hour.
    Args:
        df - pd.DataFrame containing trajectories
    '''
    fig = plt.figure()
    ax = fig.add_subplot(111, projection='3d')
    for i in df.index.unique():
        ax.plot(df.loc[i].x.values, df.loc[i].y.values, df.loc[i].t.values)
    plt.show()


def traj_2d(df, show=True):
    '''
    Plots trajectories in 2D using matplotlib
    Args:
        df - pd.DataFrame containing trajectories
    '''
    fig, ax = plt.subplot()
    for i in df.index.unique():
        ax.plot(df.loc[i].x.values, df.loc[i].y.values)
    if show is True:
        plt.show()

    return fig


def calculate_velocity(df, in_place=True):
    '''
    Calculates angle, speed and rotation at each time point
    Args:
        df - DataFrame indexed by 'traj'
        in_place - process df in place and return None
    Returns:
        dataframe with two more columns, 'angle' and 'speed' (if in_place=False)
    '''
    # Calculate angle
    pos_diff = df[['x', 'y']][1:].values - df[['x', 'y']][:-1].values
    df['angle'] = np.insert(np.arctan2(pos_diff[:, 1], pos_diff[:, 0]), 0,
                            np.nan)

    # Time difference
    t_diff = df.t[1:].values - df.t[:-1].values

    # Calculate speed
    pos_diff = pos_diff ** 2
    df['speed'] = np.insert(np.sqrt(pos_diff[:, 0] + pos_diff[:, 1]) / t_diff,
                            0, np.nan)

    # Calculate rotation rate
    rot = np.mod(df.angle[1:].values - df.angle[:-1].values, 2 * np.pi)
    rot[rot > np.pi] -= 2 * np.pi
    df['rotation'] = np.insert(rot / t_diff, 0, np.nan)

    # Remove velocity calculations between trajectories
    for i in df.index.unique():
        df.loc[i].iloc[0][['angle', 'speed', 'rotation']] = np.nan
    return None


def calculate_distances(df):
    '''
    Calculates distance between bees at each timestep.
    Args:
        df - DataFrame with with column headings 't', 'x', 'y' and indexed by
        'traj'. May also have additional columns (eg. for velocity).
    Returns:
        DataFrame indexed by time, with column 'd' containing the euclidean
        distance at that time step.
    '''
    dft = df.reset_index().set_index('t').sort_index()
    print 'Determining two-bee times.'
    ddf = pd.DataFrame(index=dft[1:].loc[dft.index[1:] == dft.index[:-1]].index,
                       columns=['d'])
    assert len(ddf.index) > 0
    assert not ddf.index.has_duplicates
    print 'Applying time index.'
    dft = dft.loc[ddf.index]

    print 'Calculating Euclidean Distances.'
    sq_diff_coords = (dft.iloc[1::2][['x', 'y']].values -
                      dft.iloc[::2][['x', 'y']].values) ** 2
    ddf.d = np.sqrt(sq_diff_coords[:, 0] + sq_diff_coords[:, 1])

    print 'Done.'
    return ddf


def process_trajectories(traj_dir, cond_file, out_dir, time_offset=9,
                         min_length=2, trim_start_frames=0, trim_end_frames=0):
    '''
    Parses trajectory files, trims, calculates velocity and bee distances when
    there are 2 bees. Then writes resulting dataframes to csv files for each
    condition in each day. This is quite memory intensive and will take a while.
    Args:
        traj_dir - path of directory where raw trajectory files are stored
        cond_file - path of conditions file
        out_dir - path to output subdirectories in
        time_offset - passed to get_filenames. Determines time cutoff for
            overnight filming
        min_length - passed to filter_traj
        trim_start_frame - passed to filter_traj
        trim_end_frames - passed to filter_traj
    Returns:
        None
    '''
    cond_beenum = {1: 1, 2: 2, 3: 2, 4: 4}
    files = get_filenames(traj_dir, cond_file, time_offset=time_offset)
    for c in files.index.levels[0]:
        for d in files.index.levels[1]:
            print 'Processing condtion %s, %s' % (c, d.date())
            df = combine_traj_files(files.loc[c, d].values.flat, cond_beenum[c])
            df = filter_traj(df, min_length=min_length,
                             trim_start_frames=trim_start_frames,
                             trim_end_frames=trim_end_frames)
            if cond_beenum[c] == 2:
                ddf = calculate_distances(df)
                ddf.to_csv('/'.join(
                    [out_dir, 'cond%s' % c, 'distance', '%s.csv' % d.date()]))
                del ddf
            calculate_velocity(df, in_place=True)
            df.to_csv('/'.join(
                [out_dir, 'cond%s' % c, 'trajectory', '%s.csv' % d.date()]))
            del df
            gc.collect()

    print 'Done.'
    return None


def radius_hist(df, bins=25, centre=None, show=True):
    '''
    Plots a histogram of the proportion of time spent at varying distance from
    the centre of the petri-dish.
    Args:
        df - trajectory dataframe
        bins - number of bins
        centre - x and y coordinates of centre of dish. If None, calculated as
                 0.5 * df.x.max(), 0.5 * df.y.max()
        show - show histogram, else just return it
    Returns:
        r, histogram
    '''
    if centre is None:
        centre = 0.5 * df.x.max(), 0.5 * df.y.max()

    r = np.sqrt((df.x.values - centre[0]) ** 2 + (df.y.values - centre[1]) ** 2)
    h = plt.hist(r, bins=bins, normed=True)
    if show is True:
        plt.show()

    return r, h


def produce_fig(df, distance_df=None, title=None, show=True):
    '''
    Produces figures for a given DataFrame
    Args:
        df - trajectory DataFrame
        distance_df - optional distance DataFrame
        title - Main figure title
        show - show figure
    Returns:
        fig
    '''
    fig = plt.figure(figsize=(12, 8), tight_layout={'pad': 2.0})

    # 2D plot of trajectories
#    plt.subplot(321)
#    for i in df.index.unique():
#        plt.plot(df.loc[i].x.values, df.loc[i].y.values)
#    plt.title('Trajectories')

    # 3D plot of trajectories
    plt.subplot(231, projection='3d')
    for i in df.index.unique():
        plt.plot(df.loc[i].x.values, df.loc[i].y.values, df.loc[i].t.values)
    plt.title('Trajectories')
    plt.tick_params(axis='both', which='major', labelsize=8)

    # 2D Histogram of position frequencies
    plt.subplot(232)
    plt.hist2d(df.x, df.y, bins=100, norm=LogNorm())
    plt.colorbar()
    plt.title('Pos Heatmap')

    # Histogram of distance from centre
    plt.subplot(233)
    radius_hist(df, show=False)
    plt.title('Distance from Centre')

    # Histogram of speed
    plt.subplot(234)
    plt.hist(df.speed.dropna().values, bins=int(df.speed.max()),
             normed=True, histtype='step', log=True)
    plt.title('Speed Distribution')

    # Histogram of velocities
    plt.subplot(235, polar=True)
    vh = np.histogram2d(df.speed.dropna().values, df.angle.dropna().values,
                        bins=100, normed=True)
    plt.pcolormesh(vh[2], vh[1], vh[0], norm=LogNorm())
    plt.colorbar(pad=0.075)
    plt.title('Velocity Heatmap')
    plt.tick_params(axis='both', which='major', labelsize=8)

    # Pairwise distance histogram
    if distance_df is not None:
        plt.subplot(236)
        plt.hist(distance_df.values, bins=50, normed=True)
        plt.title('Pairwise Distance')

    plt.suptitle(title)

    if show is True:
        plt.show()

    return fig


def fig_from_vars(condition, date_str, directory='ProcessedFiles', show=True):
    '''
    Wrapper for produce_fig
    Args:
        condition - condition int (1, 2, 3 or 4)
        date_str - date in format YYYY-MM-DD
        directory - directory csv files are stored in
        show - show figure
    Returns:
        fig
    '''
    print 'Loading trajectory file.'
    df = pd.read_csv('%s/cond%s/trajectory/%s.csv' % (directory, condition,
                                                      date_str),
                     index_col='traj')
    if condition in {2, 3}:
        print 'Loading distance file.'
        ddf = pd.read_csv('%s/cond%s/distance/%s.csv'
                          % (directory, condition, date_str), index_col='t')
    else:
        ddf = None

    print 'Producing figure.'
    fig = produce_fig(df, distance_df=ddf, show=show,
                      title='Condition %s %s' % (condition, date_str))
    return fig


def all_figs(pdf_name, conditions=[1, 2, 3, 4], data_dir='ProcessedFiles'):
    '''
    Produces ands saves figures as a pdf file.
    Args:
        pdf_name - path of pdf file
        conditions - conditions to process
        data_dir - directory where data is stored
    Returns:
        None
    '''
    pdf_file = PdfPages(pdf_name)
    for condition in conditions:
        date_list = []
        for f in os.listdir('%s/cond%s/trajectory' % (data_dir, condition)):
            if f[-4:] == '.csv':
                date_list.append(f[:-4])

        for date_str in date_list:
            print 'Condition %s %s' % (condition, date_str)
            fig = fig_from_vars(condition, date_str, directory=data_dir,
                                show=False)
            pdf_file.savefig()
            del fig

    pdf_file.close()


def produce_process_vid(movie_path, roi, out_file, dur, discard=0, sigma=16,
                        scale=1.0, thresh_kernel_size=101, fps=25.0, show=True):
    '''
    Performs a mock processing of movie and saves a new movie displaying 4
    stages of the process.
    Args:
        movie_path - path of movie
        roi - region of interest
        out_file - path of output video
        dur - duration to record (in frames)
        discard - number of frames to discard from start of file
        sigma - sigma for laplacian of a gaussian kernel
        scale - scale images
        thresh_kernel_size - change kernel size for adaptive threshold
        fps - fps to save video as
        show - show video progress
    '''
    circlemask = get_roi_mask(roi, scale)
    k = get_log_kernel(scale * sigma)
    cap = cv2.VideoCapture(movie_path)
    fourcc = cv2.VideoWriter_fourcc(*'DIVX')
    thresh_k_size = get_thresh_kernel_size(roi, scale)

    for i in range(discard):
        ret, frame = cap.read()
    imsize = (int((roi[2] - roi[0]) * 2 * scale + 1),
              int((roi[3] - roi[1]) * 2 * scale + 1))
    out = cv2.VideoWriter(out_file, fourcc, fps, imsize)

    for i in range(dur):
        print '\r%s / %s' % (i, dur),
        ret, frame = cap.read()
        if not ret:
            break
        p = process_frame(frame, 4, k, roi=roi, roi_mask=circlemask,
                          scale=scale, thresh_kernel_size=thresh_k_size)
        p0 = cv2.dilate((p[0] * 255).astype(np.uint8), np.ones((5, 5)))
        vline = np.ones((p0.shape[0], 1), dtype=np.uint8) * 255
        hline = np.ones((1, p0.shape[1] * 2 + 1), dtype=np.uint8) * 255
        joined = np.vstack((np.hstack((p[5], vline, p[3])), hline,
                            np.hstack((p[2], vline, p0))))
        color = cv2.cvtColor(joined, cv2.COLOR_GRAY2BGR)
        out.write(color)
        if show:
            cv2.imshow('Combined Image', color)
            if cv2.waitKey(1) == ord('q'):
                break
    cap.release()
    out.release()
    cv2.destroyAllWindows()
    return None


def id_dead(df):
    '''
    Identifies dead bee in an isolated + dead experiement.
    Args:
        df - trajectory DataFrame
    Returns:
        list of trajectory ideces (dead), list of trajectory indeces (alive)
    '''
    pass


def perc_t_moving(file_path, threshold=1):
    '''
    Calculate the proportion of time bees are moving
    Args:
        file_path - str containing path of processed trajectory csv file
        threshold - velocity threshold for determining if bee is moving
    Returns:
        float - proportion of time moving
    '''
    df = pd.read_csv(file_path, index_col='traj')
    return float(len(df[df.speed > threshold])) / float(len(df))


def all_perc_t_moving(directory, conditions=[1, 2, 3, 4], threshold=1):
    '''
    Call perc_t_moving on all processed trajectory files in directory
    Args:
        directory - path of parent directory where trajectory files are stored
                    should have structure:
                        directory -|-cond1/trajectory/
                                   |-cond2/trajectory/
                                   |-etc...
        threshold - passed to perc_t_moving
    Returns:
        DataFrame indexed by 'cond', 'date', with values 'perc_t_moving'
    '''
    conditions_list = []
    date_list = []
    perc_list = []

    for i in conditions:
        print 'Condition %s' % i
        path_str = '%s/cond%s/trajectory' % (directory, i)
        files = []
        for f in os.listdir(path_str):
            if f[-4:] == '.csv':
                files.append('/'.join((path_str, f)))

        for f in files:
            print 'Processing %s' % f
            prop = perc_t_moving(f, threshold=threshold)
            conditions_list.append(i)
            date_list.append(f[-14:-4])
            perc_list.append(prop)

    print 'Making DataFrame'
    df = pd.DataFrame({'cond': conditions_list, 'date': date_list,
                       'perc_t_moving': perc_list}).set_index(['cond', 'date'])
    return df


def parse_args():
    parser = argparse.ArgumentParser(description='''Perform various post
                                     processing steps on trajectory data and
                                     get some statistics.''')
    parser.add_argument('-i', type=str, metavar='DFfile', help='''Dataframe file
                        to be read in.''', default='')

    return parser.parse_args()


def main():
    args = parse_args()
    if len(args.i) > 0:
        df = pd.read_csv(args.i, index_col='traj')
    return df

if __name__ == "__main__":
    main()
